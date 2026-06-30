import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM_BASE_URL = (
    os.environ.get("UPSTREAM_BASE_URL")
    or os.environ.get("LLM_BASE_URL")
    or os.environ.get("VLLM_BASE_URL")
    or "http://vllm:8000/v1"
).rstrip("/")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("GATEWAY_PORT", "8010"))
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "4"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "20"))
UPSTREAM_RETRIES = int(os.environ.get("UPSTREAM_RETRIES", "6"))
UPSTREAM_RETRY_DELAY_SECONDS = float(os.environ.get("UPSTREAM_RETRY_DELAY_SECONDS", "2"))
FETCH_MAX_BYTES = int(os.environ.get("FETCH_MAX_BYTES", "1048576"))
FETCH_MAX_CHARS = int(os.environ.get("FETCH_MAX_CHARS", "12000"))
SEARCH_RESULT_LIMIT = int(os.environ.get("SEARCH_RESULT_LIMIT", "5"))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "qwen-14b")
AUTO_SEARCH = os.environ.get("AUTO_SEARCH", "true").lower() not in {"0", "false", "no"}
MODEL_LIST_FALLBACK = os.environ.get("MODEL_LIST_FALLBACK", "true").lower() not in {"0", "false", "no"}

SYSTEM_TOOL_HINT = (
    "You can use tools for current web information. "
    "Use search_web for discovery and fetch_url to read a result page. "
    "Cite source URLs in the final answer when web tools are used. "
    "Do not print tool call XML or JSON in the answer. "
    "Do not ask the user to wait; call a tool or answer with the evidence already provided."
)

BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            text = " ".join((data or "").split())
            if text:
                self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)


def now_ms():
    return int(time.time() * 1000)


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True


def stream_chat_response(handler, response):
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        chunk_base = {
            "id": response.get("id", "chatcmpl-local"),
            "object": "chat.completion.chunk",
            "created": response.get("created", int(time.time())),
            "model": response.get("model", DEFAULT_MODEL),
        }
        role_chunk = dict(chunk_base)
        role_chunk["choices"] = [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ]
        handler.wfile.write("data: {}\n\n".format(json.dumps(role_chunk, ensure_ascii=False)).encode("utf-8"))
        handler.wfile.flush()

        content_chunk = dict(chunk_base)
        content_chunk["choices"] = [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ]
        handler.wfile.write("data: {}\n\n".format(json.dumps(content_chunk, ensure_ascii=False)).encode("utf-8"))
        handler.wfile.flush()

        done_chunk = dict(chunk_base)
        done_chunk["choices"] = [
            {
                "index": 0,
                "delta": {},
                "finish_reason": choice.get("finish_reason") or "stop",
            }
        ]
        handler.wfile.write("data: {}\n\n".format(json.dumps(done_chunk, ensure_ascii=False)).encode("utf-8"))
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True


def fallback_models_response():
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "local-llm-agent-gateway",
            }
        ],
    }


def read_json_request(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def request_json(url, payload=None, timeout=HTTP_TIMEOUT_SECONDS):
    last_error = None
    for attempt in range(UPSTREAM_RETRIES + 1):
        if payload is None:
            data = None
            method = "GET"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "POST"
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method=method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
            return json.loads(text)
        except (ConnectionRefusedError, TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt >= UPSTREAM_RETRIES:
                break
            time.sleep(UPSTREAM_RETRY_DELAY_SECONDS)
    raise last_error


def open_url(url, timeout=HTTP_TIMEOUT_SECONDS, max_bytes=FETCH_MAX_BYTES):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "local-llm-agent-gateway/1.0",
            "Accept": "text/html,text/plain,application/xhtml+xml,application/json;q=0.8,*/*;q=0.2",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        text = raw.decode("utf-8", errors="replace")
    return content_type, text, truncated


def normalize_hostname(hostname):
    return (hostname or "").strip().rstrip(".").lower()


def assert_public_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are allowed.")
    host = normalize_hostname(parsed.hostname)
    if not host or host in BLOCKED_HOSTS:
        raise ValueError("Blocked host.")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = [item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)]
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ValueError("Blocked private or non-public address.")


def tool_search_web(arguments):
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = int(arguments.get("limit") or SEARCH_RESULT_LIMIT)
    limit = max(1, min(limit, SEARCH_RESULT_LIMIT))
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    separator = "&" if "?" in SEARXNG_URL else "?"
    data = request_json(SEARXNG_URL + separator + params)
    results = []
    for item in data.get("results", [])[:limit]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "engine": item.get("engine", ""),
            }
        )
    return {"query": query, "results": results}


def html_to_text(html):
    parser = TextExtractor()
    parser.feed(html)
    return parser.text()


def tool_fetch_url(arguments):
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    assert_public_url(url)
    content_type, body, truncated = open_url(url)
    if "html" in content_type.lower():
        text = html_to_text(body)
    else:
        text = body
    text = text[:FETCH_MAX_CHARS]
    return {"url": url, "content_type": content_type, "truncated": truncated, "text": text}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the public web with SearXNG and return result titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {"type": "integer", "description": "Maximum result count, 1-5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch readable text from a public http/https URL. Private, loopback, and link-local hosts are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public URL to read."},
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "search_web": tool_search_web,
    "fetch_url": tool_fetch_url,
}


def parse_tool_arguments(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(value)


def normalize_text_tool_call(data, index):
    if not isinstance(data, dict):
        return None
    function = data.get("function") if isinstance(data.get("function"), dict) else data
    name = function.get("name")
    if name not in TOOL_HANDLERS:
        return None
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        argument_text = arguments
    else:
        argument_text = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": "text-tool-call-{}".format(index),
        "type": "function",
        "function": {
            "name": name,
            "arguments": argument_text,
        },
    }


def parse_text_tool_calls(content):
    if not isinstance(content, str) or "<tool_call>" not in content:
        return []
    calls = []
    matches = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", content, flags=re.DOTALL)
    for index, raw in enumerate(matches):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        call = normalize_text_tool_call(parsed, index)
        if call:
            calls.append(call)
    return calls


def run_tool_call(tool_call):
    function = tool_call.get("function") or {}
    name = function.get("name") or ""
    arguments = parse_tool_arguments(function.get("arguments"))
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError("Unknown tool: {}".format(name))
    return handler(arguments)


def with_gateway_tools(payload):
    payload = dict(payload)
    payload.setdefault("model", DEFAULT_MODEL)
    payload["stream"] = False
    client_tools = payload.get("tools") or []
    payload["tools"] = TOOLS + [tool for tool in client_tools if tool.get("type") == "function"]
    payload.setdefault("tool_choice", "auto")
    payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
    messages = list(payload.get("messages") or [])
    messages.insert(0, {"role": "system", "content": SYSTEM_TOOL_HINT})
    add_auto_search_context(messages)
    payload["messages"] = messages
    return payload


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


def should_auto_search(text):
    if not AUTO_SEARCH:
        return False
    lowered = (text or "").lower()
    triggers = [
        "web",
        "search",
        "latest",
        "current",
        "today",
        "recent",
        "웹",
        "검색",
        "최신",
        "현재",
        "오늘",
        "최근",
        "뉴스",
        "주가",
        "시세",
        "가격",
        "증시",
        "코스피",
        "코스닥",
        "삼성전자",
        "삼전",
        "환율",
        "달러",
        "원화",
        "얼마",
    ]
    return any(trigger in lowered for trigger in triggers)


def add_auto_search_context(messages):
    query = last_user_text(messages)
    if not should_auto_search(query):
        return
    try:
        result = tool_search_web({"query": query, "limit": SEARCH_RESULT_LIMIT})
        messages.append(
            {
                "role": "system",
                "content": "Auto web search results:\n{}".format(json.dumps(result, ensure_ascii=False)),
            }
        )
    except Exception as exc:
        messages.append(
            {
                "role": "system",
                "content": "Auto web search failed: {}".format(exc),
            }
        )


def append_tool_results(messages, response):
    message = response.get("choices", [{}])[0].get("message", {})
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        tool_calls = parse_text_tool_calls(message.get("content"))
        if not tool_calls:
            return False
        message = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    messages.append(message)
    for tool_call in tool_calls:
        try:
            result = run_tool_call(tool_call)
            content = json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except Exception as exc:
            content = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "name": (tool_call.get("function") or {}).get("name", ""),
                "content": content,
            }
        )
    return True


def chat_completions(payload):
    agent_payload = with_gateway_tools(payload)
    agent_payload["stream"] = False
    messages = agent_payload["messages"]
    for _ in range(MAX_TOOL_ROUNDS):
        response = request_json(UPSTREAM_BASE_URL + "/chat/completions", agent_payload)
        if not append_tool_results(messages, response):
            return response
    agent_payload.pop("tools", None)
    agent_payload["tool_choice"] = "none"
    messages.append(
        {
            "role": "system",
            "content": "Tool round limit reached. Answer with the evidence already collected.",
        }
    )
    return request_json(UPSTREAM_BASE_URL + "/chat/completions", agent_payload)


class Handler(BaseHTTPRequestHandler):
    server_version = "local-llm-agent-gateway/1.0"

    def log_message(self, fmt, *args):
        print("{} - {}".format(self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "upstream_base_url": UPSTREAM_BASE_URL,
                    "searxng_url": SEARXNG_URL,
                },
            )
            return
        if path == "/v1/models":
            try:
                json_response(self, 200, request_json(UPSTREAM_BASE_URL + "/models"))
            except Exception as exc:
                if MODEL_LIST_FALLBACK:
                    payload = fallback_models_response()
                    payload["agent_gateway_warning"] = str(exc)
                    json_response(self, 200, payload)
                else:
                    json_response(self, 502, {"error": str(exc)})
            return
        json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        started = now_ms()
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path != "/v1/chat/completions":
            json_response(self, 404, {"error": "not found"})
            return
        try:
            payload = read_json_request(self)
            wants_stream = bool(payload.get("stream"))
            response = chat_completions(payload)
            response.setdefault("agent_gateway", {})["latency_ms"] = now_ms() - started
            if wants_stream:
                stream_chat_response(self, response)
                return
            json_response(self, 200, response)
        except json.JSONDecodeError as exc:
            json_response(self, 400, {"error": "invalid json", "detail": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except (OSError, urllib.error.URLError) as exc:
            json_response(self, 502, {"error": str(exc)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("agent-gateway listening on {}:{} -> {}".format(HOST, PORT, UPSTREAM_BASE_URL), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
