"""Agent gateway: the API entrance, a thin tool-calling loop in front of llm-scheduler.

    client -> agent-gateway -> llm-scheduler -> backend (strata | vllm | ollama)
                   |
                   +-> tool-service  (web search, URL fetch, repository files / P4)

The gateway does not decide whether a question needs a tool. It hands the model the tool
schemas (fetched from tool-service) and lets the model ask for one with a structured
`tool_calls` response. The gateway runs that call through tool-service, feeds the result
back, and repeats. A request the model answers without a tool is relayed untouched, with
real streaming. Anything it does not handle itself (`/v1/models`, embeddings, the scheduler
status page, ...) is relayed to the scheduler as is.

Rules for tool calls:
  * only tools owned by tool-service are executed here; a tool the client declared itself
    is returned to the client unchanged;
  * the client's own `tools` are kept, and `tool_choice` other than "auto" turns gateway
    tools off for that request, as does the header `X-Agent-Tools: off`;
  * Open WebUI's background prompts ("### Task:") never get tools.

Two listeners share the code:
  * api       (GATEWAY_PORT, published): priority and caller come from the client's
              `X-LLM-Priority` / `X-Caller` headers, else the scheduler's defaults.
  * internal  (GATEWAY_INTERNAL_PORT, Docker network only, used by Open WebUI): fixed
              LLM_PRIORITY / LLM_CALLER and thinking off by default.
"""
import http.client
import json
import os
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM_BASE_URL = (
    os.environ.get("UPSTREAM_BASE_URL")
    or os.environ.get("LLM_BASE_URL")
    or os.environ.get("VLLM_BASE_URL")
    or "http://vllm:8000/v1"
).rstrip("/")
TOOL_SERVICE_URL = os.environ.get("TOOL_SERVICE_URL", "").rstrip("/")
TOOLS_ENABLED = os.environ.get("GATEWAY_TOOLS_ENABLED", "true").lower() not in {"0", "false", "no"}
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8000"))
INTERNAL_PORT = int(os.environ.get("GATEWAY_INTERNAL_PORT", "8010"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "4"))
# The internal listener (Open WebUI) queues as interactive by default.
LLM_PRIORITY = os.environ.get("LLM_PRIORITY", "P0")
LLM_CALLER = os.environ.get("LLM_CALLER", "open-webui")
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "20"))
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "600"))
TOOL_TIMEOUT_SECONDS = int(os.environ.get("TOOL_TIMEOUT_SECONDS", "700"))
TOOL_CACHE_SECONDS = float(os.environ.get("TOOL_CACHE_SECONDS", "30"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(64 * 1024 * 1024)))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "qwen-14b")
MODEL_LIST_FALLBACK = os.environ.get("MODEL_LIST_FALLBACK", "true").lower() not in {"0", "false", "no"}

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "content-length", "host"}
# Headers that are the gateway's own business and are not forwarded to the scheduler.
GATEWAY_ONLY_HEADERS = {"x-agent-tools"}
SYNTHETIC_FINISH = "stop"


class ClientGone(Exception):
    """The caller closed its connection; stop work for it."""


class UpstreamError(Exception):
    """The scheduler or backend could not be reached."""


class EmptyCompletionError(RuntimeError):
    """Upstream answered, but with nothing in it."""


def log(message):
    print(message, flush=True)


def now_ms():
    return int(time.time() * 1000)


# --------------------------------------------------------------------------------------
# Upstream (llm-scheduler)
# --------------------------------------------------------------------------------------

def _split(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname, parsed.port or 80, parsed.path.rstrip("/")


UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_PREFIX = _split(UPSTREAM_BASE_URL)
# `/v1/...` requests are relayed to <root>/v1/..., so strip the /v1 that UPSTREAM_BASE_URL ends with.
UPSTREAM_ROOT_PREFIX = UPSTREAM_PREFIX[:-3] if UPSTREAM_PREFIX.endswith("/v1") else UPSTREAM_PREFIX


def client_closed(sock):
    """True when the peer already closed the connection (checked without consuming data)."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def open_upstream(method, path, body, headers, timeout, client_sock=None):
    """Send a request and return (connection, response) once the response headers arrive.

    While the scheduler holds the request in its queue nothing comes back, so the caller's
    socket is watched: if it goes away the upstream connection is closed, which makes the
    scheduler drop the queued request instead of generating for nobody.
    """
    conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=timeout)
    box = {}

    def work():
        try:
            conn.request(method, path, body=body, headers=headers)
            box["resp"] = conn.getresponse()
        except BaseException as exc:  # reported to the waiting thread
            box["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(1.0)
        if worker.is_alive() and client_sock is not None and client_closed(client_sock):
            conn.close()
            raise ClientGone()
    if "error" in box:
        conn.close()
        raise UpstreamError(str(box["error"]) or type(box["error"]).__name__)
    return conn, box["resp"]


def upstream_headers(handler, listener, extra=None):
    """Headers for the scheduler: the caller's, minus hop-by-hop, plus identity."""
    headers = {k: v for k, v in handler.headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() not in GATEWAY_ONLY_HEADERS
               and k.lower() not in {"x-forwarded-for", "accept-encoding"}}
    # The scheduler sees the gateway as its client; hand it the real address for its IP rules.
    headers["X-Forwarded-For"] = handler.client_address[0]
    if listener["priority"]:
        headers["X-LLM-Priority"] = listener["priority"]
    if listener["caller"]:
        headers["X-Caller"] = listener["caller"]
    headers.update(extra or {})
    return headers


# --------------------------------------------------------------------------------------
# Tool service
# --------------------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self, url, cache_seconds):
        self.url, self.cache_seconds = url, cache_seconds
        self.lock = threading.Lock()
        self.tools_cache, self.fetched, self.failing = [], 0.0, False

    def tools(self):
        if not self.url:
            return []
        with self.lock:
            if time.monotonic() - self.fetched < self.cache_seconds:
                return list(self.tools_cache)
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        self.url + "/tools", timeout=HTTP_TIMEOUT_SECONDS) as response:
                    data = json.loads(response.read().decode("utf-8", errors="replace"))
                self.tools_cache = [t for t in data.get("tools") or []
                                    if t.get("type") == "function" and (t.get("function") or {}).get("name")]
                self.failing = False
            except (OSError, ValueError, urllib.error.URLError) as exc:
                if not self.failing:
                    log("tool-service unavailable, serving without gateway tools: {}".format(exc))
                self.failing = True
                self.tools_cache = []
            self.fetched = time.monotonic()
            return list(self.tools_cache)

    def call(self, name, arguments, chat_id):
        body = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url + "/call", data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "X-Chat-Id": chat_id})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    request, timeout=TOOL_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"ok": False, "error": "tool service failed: {}".format(exc)}


REGISTRY = ToolRegistry(TOOL_SERVICE_URL, TOOL_CACHE_SECONDS)


def run_tool_calls(calls, chat_id):
    """Execute the model's gateway-owned calls; returns the `tool` messages to append."""
    messages = []
    for call in calls:
        function = call.get("function") or {}
        name = function.get("name") or ""
        started = now_ms()
        try:
            text = function.get("arguments")
            arguments = text if isinstance(text, dict) else (json.loads(text) if text else {})
            result = REGISTRY.call(name, arguments, chat_id)
        except ValueError as exc:
            result = {"ok": False, "error": "invalid tool arguments: {}".format(exc)}
        log("tool {} {} {}ms".format(name, "ok" if result.get("ok") else "failed", now_ms() - started))
        messages.append({
            "role": "tool",
            "tool_call_id": call.get("id", ""),
            "name": name,
            "content": json.dumps(result, ensure_ascii=False),
        })
    return messages


# --------------------------------------------------------------------------------------
# Request shaping
# --------------------------------------------------------------------------------------

def last_user_text(messages):
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "\n".join(parts).strip()
    return ""


def is_meta_task_prompt(text):
    stripped = (text or "").lstrip()
    return stripped.startswith("### Task:")


def tools_wanted(payload, headers):
    """Whether this request may be given the gateway's tools."""
    if not TOOLS_ENABLED or not TOOL_SERVICE_URL:
        return False
    if str(headers.get("X-Agent-Tools", "")).strip().lower() in {"off", "0", "false", "no"}:
        return False
    if payload.get("tool_choice") not in (None, "auto"):
        return False
    return not is_meta_task_prompt(last_user_text(payload.get("messages") or []))


def normalize_messages(messages):
    # Qwen templates permit a single system message at the beginning.
    # Keep conversation/tool ordering and preserve structured content parts.
    systems = []
    conversation = []
    for message in messages or []:
        if message.get("role") == "system":
            systems.append(message.get("content") or "")
        else:
            conversation.append(message)
    if systems:
        if all(isinstance(content, str) for content in systems):
            content = "\n\n".join(systems)
        else:
            content = []
            for part in systems:
                if content:
                    content.append({"type": "text", "text": "\n\n"})
                content.extend(part if isinstance(part, list) else [{"type": "text", "text": part}])
        conversation.insert(0, {"role": "system", "content": content})
    return conversation


def build_upstream_payload(payload, messages, listener, gateway_tools, limit_reached, stream):
    up = dict(payload)
    up["messages"] = normalize_messages(messages)
    up["stream"] = stream
    for key, value in listener["defaults"].items():
        up.setdefault(key, value)
    up.setdefault("model", DEFAULT_MODEL)
    client_tools = list(payload.get("tools") or [])
    if gateway_tools:
        declared = {(t.get("function") or {}).get("name") for t in client_tools}
        up["tools"] = client_tools + [t for t in gateway_tools if t["function"]["name"] not in declared]
        up.setdefault("tool_choice", "auto")
    elif limit_reached and not client_tools:
        up.pop("tools", None)
        up["tool_choice"] = "none"
    return up


def new_call_id():
    return "call_{}".format(uuid.uuid4().hex[:24])


# --------------------------------------------------------------------------------------
# Client output
# --------------------------------------------------------------------------------------

def encode_json(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Sink:
    """What the caller sees: one JSON body, or one SSE stream that may span several rounds."""

    def __init__(self, handler):
        self.handler = handler
        self.started = False
        self.finished = False

    def _write(self, data):
        try:
            self.handler.wfile.write(data)
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.handler.close_connection = True
            raise ClientGone()

    def json(self, status, body, content_type="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else encode_json(body)
        try:
            self.handler.send_response(status)
            self.handler.send_header("Content-Type", content_type)
            self.handler.send_header("Content-Length", str(len(raw)))
            self.handler.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.handler.close_connection = True
            raise ClientGone()
        self._write(raw)
        self.started = self.finished = True

    def start_stream(self):
        if self.started:
            return
        try:
            self.handler.send_response(200)
            self.handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.handler.send_header("Cache-Control", "no-cache")
            self.handler.send_header("Connection", "close")
            self.handler.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.handler.close_connection = True
            raise ClientGone()
        self.handler.close_connection = True
        self.started = True

    def event(self, raw):
        self.start_stream()
        self._write(raw)

    def comment(self, text):
        """SSE comment: keeps idle proxies from timing out while a tool runs."""
        if self.started and not self.finished:
            self._write(": {}\n\n".format(text).encode("utf-8"))

    def finish(self):
        if self.finished:
            return
        self.start_stream()
        self._write(b"data: [DONE]\n\n")
        self.finished = True


# --------------------------------------------------------------------------------------
# One round against the model
# --------------------------------------------------------------------------------------

class Outcome:
    """`calls` is None when the reply was delivered to the caller, else the gateway calls to run."""

    def __init__(self, calls=None, assistant=None):
        self.calls, self.assistant = calls, assistant


def assert_non_empty(response):
    # A GPU that has fallen off the bus keeps returning HTTP 200 with an empty
    # completion instead of raising, so a dead card reads as a healthy one for
    # as long as nobody looks at the text. On 2026-08-24 that gap was over three
    # hours. An empty answer has to fail here or it fails nowhere.
    for choice in response.get("choices") or []:
        message = choice.get("message") or {}
        if (message.get("content") or "").strip() or message.get("tool_calls"):
            return
    raise EmptyCompletionError(
        "upstream returned an empty completion; check the GPU with nvidia-smi "
        "and 'journalctl -k -b 0 | grep Xid' before retrying"
    )


def post_chat(handler, listener, up):
    body = encode_json(up)
    headers = upstream_headers(handler, listener, {"Content-Type": "application/json; charset=utf-8"})
    return open_upstream("POST", UPSTREAM_PREFIX + "/chat/completions", body, headers,
                         LLM_TIMEOUT_SECONDS, handler.connection)


def plain_round(handler, listener, up, active, sink):
    conn, resp = post_chat(handler, listener, up)
    try:
        raw = resp.read()
        status = resp.status
        content_type = resp.getheader("Content-Type") or "application/json; charset=utf-8"
    finally:
        conn.close()
    if status != 200:
        sink.json(status, raw, content_type)
        return Outcome()
    try:
        response = json.loads(raw)
    except ValueError:
        sink.json(200, raw, content_type)
        return Outcome()
    assert_non_empty(response)
    message = ((response.get("choices") or [{}])[0]).get("message") or {}
    calls = message.get("tool_calls") or []
    owned = [c for c in calls if (c.get("function") or {}).get("name") in active]
    if owned and len(owned) == len(calls):
        for call in calls:
            call.setdefault("id", new_call_id())
        return Outcome(calls, message)
    sink.json(200, raw, content_type)
    return Outcome()


def iter_sse(resp):
    """Yield (raw event bytes, data text) for each server-sent event."""
    lines = []
    while True:
        line = resp.readline()
        if not line:
            if lines:
                yield b"".join(lines), _event_data(lines)
            return
        if line in (b"\n", b"\r\n"):
            if lines:
                yield b"".join(lines) + b"\n", _event_data(lines)
                lines = []
            continue
        lines.append(line)


def _event_data(lines):
    parts = []
    for line in lines:
        if line.startswith(b"data:"):
            parts.append(line[5:].strip().decode("utf-8", errors="replace"))
    return "\n".join(parts) if parts else None


class StreamState:
    def __init__(self):
        self.owner = {}      # tool call index -> True when the gateway runs it
        self.calls = {}      # index -> accumulated gateway call
        self.foreign = False
        self.content = ""
        self.has_output = False
        self.pending = []    # role-only chunks held back until the reply shows what it is

    def has_gateway_calls(self):
        return bool(self.calls)


def stream_round(handler, listener, up, active, sink):
    conn, resp = post_chat(handler, listener, up)
    state = StreamState()
    try:
        if resp.status != 200:
            sink.json(resp.status, resp.read(), resp.getheader("Content-Type") or "application/json; charset=utf-8")
            return Outcome()
        for raw, data in iter_sse(resp):
            if data is None:
                continue  # comment / keep-alive from upstream
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                _flush(sink, state)
                sink.event(raw)
                state.has_output = True
                continue
            _handle_chunk(sink, state, active, chunk, raw)
    finally:
        conn.close()

    if state.calls and state.foreign:
        log("model mixed gateway and client tool calls in one reply; gateway calls dropped")
    if state.calls and not state.foreign:
        calls = []
        for index in sorted(state.calls):
            call = state.calls[index]
            calls.append({
                "id": call["id"] or new_call_id(), "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
            })
        return Outcome(calls, {"role": "assistant", "content": state.content, "tool_calls": calls})
    if not state.has_output and not sink.started:
        raise EmptyCompletionError(
            "upstream returned an empty completion; check the GPU with nvidia-smi "
            "and 'journalctl -k -b 0 | grep Xid' before retrying"
        )
    _flush(sink, state)
    sink.finish()
    return Outcome()


def _flush(sink, state):
    for raw in state.pending:
        sink.event(raw)
    state.pending = []


def _handle_chunk(sink, state, active, chunk, raw):
    choices = chunk.get("choices") or []
    if not choices:
        # Usage-only chunk. Usage of a round that ends in a gateway tool call is not the caller's.
        if not state.has_gateway_calls():
            _flush(sink, state)
            sink.event(raw)
        return
    choice = choices[0]
    delta = choice.get("delta") or {}

    foreign = []
    owned = False
    for call in delta.get("tool_calls") or []:
        index = call.get("index", 0)
        function = call.get("function") or {}
        name = function.get("name")
        if index not in state.owner:
            state.owner[index] = bool(name) and name in active
            if state.owner[index]:
                state.calls[index] = {"id": "", "name": name, "arguments": ""}
        if state.owner[index]:
            owned = True
            entry = state.calls[index]
            entry["id"] = call.get("id") or entry["id"]
            entry["arguments"] += function.get("arguments") or ""
        else:
            foreign.append(call)
            state.foreign = True
            state.has_output = True

    text = delta.get("content") or ""
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
    if text:
        state.content += text
        if text.strip():
            state.has_output = True
    finish = choice.get("finish_reason")
    # The finish chunk is a separate chunk without tool_calls, so judge by the round so far.
    withhold_finish = bool(finish) and state.has_gateway_calls() and not state.foreign

    nothing_yet = not (text or reasoning or foreign or owned) and not state.has_output and not sink.started
    if not (text or reasoning or foreign or finish or owned) or (finish and nothing_yet and not state.has_gateway_calls()):
        # Role-only / empty delta, or a finish with no output before it: held back so that an
        # empty completion can still be refused with a 502 instead of a blank 200 stream.
        if not sink.started:
            state.pending.append(raw)
        return
    if owned or withhold_finish:
        # Rebuild the chunk without the gateway's own tool calls and without a tool-call finish.
        cleaned = {k: v for k, v in delta.items() if k != "tool_calls"}
        if foreign:
            cleaned["tool_calls"] = foreign
        choice = dict(choice, delta=cleaned)
        if withhold_finish:
            choice["finish_reason"] = None
        chunk = dict(chunk, choices=[choice] + list(choices[1:]))
        raw = "data: {}\n\n".format(json.dumps(chunk, ensure_ascii=False)).encode("utf-8")
        if not (text or reasoning or foreign):
            return
    _flush(sink, state)
    sink.event(raw)


def run_chat(handler, listener, payload, sink):
    wants_stream = bool(payload.get("stream"))
    gateway_tools = REGISTRY.tools() if tools_wanted(payload, handler.headers) else []
    chat_id = uuid.uuid4().hex
    messages = list(payload.get("messages") or [])
    rounds = 0
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        limit_reached = bool(gateway_tools) and round_no == MAX_TOOL_ROUNDS
        tools_now = [] if limit_reached else gateway_tools
        active = {t["function"]["name"] for t in tools_now}
        if limit_reached:
            messages.append({
                "role": "system",
                "content": "Tool round limit reached. Answer with the evidence already collected.",
            })
        up = build_upstream_payload(payload, messages, listener, tools_now, limit_reached, wants_stream)
        outcome = (stream_round if wants_stream else plain_round)(handler, listener, up, active, sink)
        if outcome.calls is None:
            if rounds:
                log("chat {} answered after {} tool round(s)".format(chat_id[:8], rounds))
            return
        rounds += 1
        messages.append(outcome.assistant)
        sink.comment("running tools")
        messages.extend(run_tool_calls(outcome.calls, chat_id))


# --------------------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------------------

def fallback_models_response():
    return {
        "object": "list",
        "data": [{"id": DEFAULT_MODEL, "object": "model", "created": 0, "owned_by": "local-llm-agent-gateway"}],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "local-llm-agent-gateway/2.0"
    listener = None

    def log_message(self, fmt, *args):
        log("{} - {}".format(self.address_string(), fmt % args))

    # ---- routing -------------------------------------------------------------------
    def _route(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return Sink(self).json(413, {"error": "request body too large"})
            body = self.rfile.read(length) if length else b""
            if self.command == "GET" and path == "/health":
                return Sink(self).json(200, {
                    "ok": True,
                    "upstream_base_url": UPSTREAM_BASE_URL,
                    "tool_service_url": TOOL_SERVICE_URL,
                    "tools": [t["function"]["name"] for t in REGISTRY.tools()],
                })
            if self.command == "GET" and path == "/v1/models":
                return self._models()
            if self.command == "POST" and path == "/v1/chat/completions":
                return self._chat(body)
            self._relay(body)
        except ClientGone:
            self.close_connection = True
        except Exception as exc:  # never leave the caller hanging on a bug
            log("gateway error on {} {}: {!r}".format(self.command, self.path, exc))
            self.close_connection = True
            try:
                Sink(self).json(500, {"error": "gateway error"})
            except ClientGone:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _route

    # ---- handlers ------------------------------------------------------------------
    def _chat(self, body):
        sink = Sink(self)
        try:
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            return sink.json(400, {"error": "invalid json", "detail": str(exc)})
        try:
            run_chat(self, self.listener, payload, sink)
        except (EmptyCompletionError, UpstreamError) as exc:
            if not sink.started:
                sink.json(502, {"error": str(exc)})
            else:
                log("chat failed after the reply started: {}".format(exc))
                sink.finish()

    def _models(self):
        sink = Sink(self)
        headers = upstream_headers(self, self.listener)
        try:
            conn, resp = open_upstream("GET", UPSTREAM_PREFIX + "/models", None, headers,
                                       HTTP_TIMEOUT_SECONDS, self.connection)
            try:
                raw, status = resp.read(), resp.status
                content_type = resp.getheader("Content-Type") or "application/json; charset=utf-8"
            finally:
                conn.close()
            if status == 200:
                return sink.json(200, raw, content_type)
            error = "upstream returned HTTP {}".format(status)
        except UpstreamError as exc:
            error = str(exc)
        if MODEL_LIST_FALLBACK:
            payload = fallback_models_response()
            payload["agent_gateway_warning"] = error
            return sink.json(200, payload)
        sink.json(502, {"error": error})

    def _relay(self, body):
        """Pass any other request to the scheduler unchanged and stream the answer back."""
        sink = Sink(self)
        headers = upstream_headers(self, self.listener)
        try:
            conn, resp = open_upstream(self.command, UPSTREAM_ROOT_PREFIX + self.path, body or None, headers,
                                       LLM_TIMEOUT_SECONDS, self.connection)
        except UpstreamError as exc:
            return sink.json(502, {"error": "upstream error: {}".format(exc)})
        try:
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        finally:
            conn.close()


def make_listener(name, priority, caller, defaults):
    return {"name": name, "priority": priority, "caller": caller, "defaults": defaults}


API_LISTENER = make_listener("api", None, None, {})
INTERNAL_LISTENER = make_listener("internal", LLM_PRIORITY, LLM_CALLER,
                                  {"chat_template_kwargs": {"enable_thinking": False}})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def serve(port, listener):
    handler = type("{}Handler".format(listener["name"].title()), (Handler,), {"listener": listener})
    server = Server((HOST, port), handler)
    log("agent-gateway {} listener on {}:{} -> {}".format(listener["name"], HOST, port, UPSTREAM_BASE_URL))
    server.serve_forever()


def main():
    if INTERNAL_PORT and INTERNAL_PORT != PORT:
        threading.Thread(target=serve, args=(INTERNAL_PORT, INTERNAL_LISTENER), daemon=True).start()
    serve(PORT, API_LISTENER)


def selfcheck():
    assert_non_empty({"choices": [{"message": {"content": "hi"}}]})
    assert_non_empty({"choices": [{"message": {"content": "", "tool_calls": [{"id": "1"}]}}]})
    for dead in ({"choices": [{"message": {"content": ""}}]},
                 {"choices": [{"message": {"content": "   "}}]},
                 {"choices": []},
                 {}):
        try:
            assert_non_empty(dead)
        except EmptyCompletionError:
            continue
        raise AssertionError("empty completion slipped through: {}".format(dead))
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
