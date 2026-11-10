"""Transparent priority scheduler in front of the Strata LLM server.

All callers (wiki-relay, agents, Jenkins, TSAISupport, Open WebUI via agent-gateway)
send OpenAI-compatible requests here. Generation requests are queued by priority
(P0 interactive > P1 pipeline > P2 batch) and forwarded unchanged to the upstream,
at most SCHED_CONCURRENCY at a time. Everything else (GET /v1/models, ...) is
passed through without queueing.

Priority comes from the `X-LLM-Priority` header (trusted as-is, LAN only). Without
it the source-IP map (SCHED_IP_PRIORITY) and then SCHED_DEFAULT_PRIORITY apply.
`X-Caller` is only a label for the status page.

Callers reach the scheduler through agent-gateway, which sends the real address in
`X-Forwarded-For`; with SCHED_TRUST_FORWARDED=true the IP rules use that address.
"""
import hashlib
import http.client
import json
import os
import re
import select
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("SCHED_LISTEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("SCHED_LISTEN_PORT", "8000"))
UPSTREAM = os.environ.get("SCHED_UPSTREAM", "http://strata:8000")
CONCURRENCY = max(1, int(os.environ.get("SCHED_CONCURRENCY", "1")))
AGING_SEC = float(os.environ.get("SCHED_AGING_SEC", "120"))
DEFAULT_PRIORITY = os.environ.get("SCHED_DEFAULT_PRIORITY", "P2")
UPSTREAM_TIMEOUT = float(os.environ.get("SCHED_UPSTREAM_TIMEOUT", "900"))
# agent-gateway is the only thing that reaches the scheduler and it reports the real caller in
# X-Forwarded-For. Only turn this on while the scheduler is not published on the host.
TRUST_FORWARDED = os.environ.get("SCHED_TRUST_FORWARDED", "false").lower() in {"1", "true", "yes"}
QUEUED_PATHS = {"/v1/chat/completions", "/v1/completions"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "content-length", "host"}
TOKENS_RE = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')
PREFIX_CHARS = 400
PREFIX_WARN_MIN_REQUESTS = 5


def parse_priority(value, default=None):
    """'P0'/'p1'/'2' -> 0..2, otherwise `default`."""
    m = re.fullmatch(r"\s*[Pp]?([0-2])\s*", str(value or ""))
    return int(m.group(1)) if m else default


def parse_ip_map(text):
    result = {}
    for item in str(text or "").split(","):
        if "=" in item:
            ip, prio = item.split("=", 1)
            level = parse_priority(prio)
            if ip.strip() and level is not None:
                result[ip.strip()] = level
    return result


IP_PRIORITY = parse_ip_map(os.environ.get("SCHED_IP_PRIORITY", ""))


def client_ip(headers, peer):
    """Address the source-IP rules apply to: the first X-Forwarded-For hop when a trusted proxy sent it."""
    if TRUST_FORWARDED:
        forwarded = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer


def resolve_priority(header_value, client_ip):
    level = parse_priority(header_value)
    if level is not None:
        return level
    if client_ip in IP_PRIORITY:
        return IP_PRIORITY[client_ip]
    return parse_priority(DEFAULT_PRIORITY, 2)


class Waiter:
    def __init__(self, priority, caller, seq):
        self.priority, self.caller, self.seq = priority, caller, seq
        self.enqueued = time.monotonic()
        self.event = threading.Event()
        self.cancelled = False

    def effective(self, now, aging_sec):
        """Waiting long enough promotes one level per aging_sec (starvation guard)."""
        if aging_sec <= 0:
            return self.priority
        return max(0, self.priority - int((now - self.enqueued) // aging_sec))


class Scheduler:
    def __init__(self, concurrency=1, aging_sec=120.0):
        self.concurrency, self.aging_sec = concurrency, aging_sec
        self.lock = threading.Lock()
        self.waiting = []
        self.running = {}
        self.seq = 0
        self.callers = {}
        self.started = time.time()

    def _stat(self, caller):
        return self.callers.setdefault(caller, {
            "requests": 0, "errors": 0, "cancelled": 0, "wait_sec": 0.0, "run_sec": 0.0,
            "completion_tokens": 0, "by_priority": {"P0": 0, "P1": 0, "P2": 0}})

    def _dispatch_locked(self):
        now = time.monotonic()
        while self.waiting and len(self.running) < self.concurrency:
            nxt = min(self.waiting, key=lambda w: (w.effective(now, self.aging_sec), w.seq))
            self.waiting.remove(nxt)
            self.running[nxt.seq] = {"caller": nxt.caller, "priority": nxt.priority, "started": now,
                                     "waited": now - nxt.enqueued}
            nxt.event.set()

    def note_prefix(self, caller, body):
        """Track the leading system prompt per caller. Strata reuses its KV cache only for an identical prefix,
        so a prefix that keeps changing (timestamps, ids) means cache misses; surfaced on the status page."""
        try:
            messages = json.loads(body).get("messages") or []
            first = messages[0] if messages else {}
            text = first.get("content") if first.get("role") == "system" else ""
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
            digest = hashlib.sha1(str(text or "")[:PREFIX_CHARS].encode("utf-8")).hexdigest()[:10] if text else ""
        except (ValueError, AttributeError, TypeError):
            return
        with self.lock:
            stat = self._stat(caller)
            stat["prefix_requests"] = stat.get("prefix_requests", 0) + 1
            if digest and stat.get("prefix_hash") and stat["prefix_hash"] != digest:
                stat["prefix_changes"] = stat.get("prefix_changes", 0) + 1
            if digest:
                stat["prefix_hash"] = digest

    def enqueue(self, priority, caller):
        with self.lock:
            self.seq += 1
            waiter = Waiter(priority, caller, self.seq)
            self.waiting.append(waiter)
            stat = self._stat(caller)
            stat["requests"] += 1
            stat["by_priority"]["P%d" % priority] += 1
            self._dispatch_locked()
            return waiter

    def cancel(self, waiter):
        """Drop a waiter whose client went away. Releases the slot if it was already granted."""
        with self.lock:
            waiter.cancelled = True
            if waiter in self.waiting:
                self.waiting.remove(waiter)
                self._stat(waiter.caller)["cancelled"] += 1
            elif waiter.seq in self.running:
                self.running.pop(waiter.seq)
                self._stat(waiter.caller)["cancelled"] += 1
                self._dispatch_locked()

    def release(self, waiter, tokens=0, error=False):
        with self.lock:
            info = self.running.pop(waiter.seq, None)
            if info:
                stat = self._stat(waiter.caller)
                stat["wait_sec"] += info["waited"]
                stat["run_sec"] += time.monotonic() - info["started"]
                stat["completion_tokens"] += tokens
                if error:
                    stat["errors"] += 1
            self._dispatch_locked()

    def status(self):
        now = time.monotonic()
        with self.lock:
            callers = {}
            for name, s in self.callers.items():
                n = max(1, s["requests"] - s["cancelled"])
                seen, changed = s.get("prefix_requests", 0), s.get("prefix_changes", 0)
                unstable = seen >= PREFIX_WARN_MIN_REQUESTS and changed / seen > 0.5
                callers[name] = dict(
                    s, avg_wait_sec=round(s["wait_sec"] / n, 2), avg_run_sec=round(s["run_sec"] / n, 2),
                    tok_per_sec=round(s["completion_tokens"] / s["run_sec"], 1) if s["run_sec"] else 0.0,
                    wait_sec=round(s["wait_sec"], 2), run_sec=round(s["run_sec"], 2),
                    prefix_unstable=unstable)
            return {
                "concurrency": self.concurrency, "aging_sec": self.aging_sec, "upstream": UPSTREAM,
                "uptime_sec": int(time.time() - self.started),
                "running": [{"caller": r["caller"], "priority": "P%d" % r["priority"],
                             "running_sec": round(now - r["started"], 1)} for r in self.running.values()],
                "queue": [{"caller": w.caller, "priority": "P%d" % w.priority,
                           "effective": "P%d" % w.effective(now, self.aging_sec),
                           "waiting_sec": round(now - w.enqueued, 1)} for w in self.waiting],
                "callers": callers,
            }


SCHEDULER = Scheduler(CONCURRENCY, AGING_SEC)


def client_closed(sock):
    """True when the peer already closed the connection (checked without consuming data)."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def split_upstream(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname, parsed.port or 80, parsed.path.rstrip("/")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "llm-scheduler/1.0"
    scheduler = SCHEDULER

    def log_message(self, fmt, *args):
        print("{} - {}".format(self.address_string(), fmt % args), flush=True)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            return self._json(200, {"ok": True, "upstream": UPSTREAM})
        if path == "/v1/scheduler/status":
            return self._json(200, self.scheduler.status())
        self._forward(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path not in QUEUED_PATHS:
            return self._forward(body)
        source = client_ip(self.headers, self.client_address[0])
        priority = resolve_priority(self.headers.get("X-LLM-Priority"), source)
        caller = (self.headers.get("X-Caller") or source).strip()[:64] or "unknown"
        self.scheduler.note_prefix(caller, body)
        waiter = self.scheduler.enqueue(priority, caller)
        while not waiter.event.wait(1.0):
            if client_closed(self.connection):
                self.scheduler.cancel(waiter)
                self.close_connection = True
                return
        self._forward(body, waiter)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = lambda self: self._forward(
        self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.headers.get("Content-Length") else None)

    def _forward(self, body, waiter=None):
        """Relay the request unchanged and stream the response back as it arrives."""
        host, port, prefix = split_upstream(UPSTREAM)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        tokens, failed, conn = 0, False, None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT)
            conn.request(self.command, prefix + self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            # Length is unknown for streamed bodies; close the connection to delimit them.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            tail = b""
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if waiter:
                    window = tail + chunk
                    found = TOKENS_RE.findall(window)
                    if found:
                        tokens = int(found[-1])
                    tail = window[-64:]
        except (BrokenPipeError, ConnectionResetError):
            failed = True
            self.close_connection = True
        except (OSError, http.client.HTTPException) as exc:
            failed = True
            self.close_connection = True
            try:
                self._json(502, {"error": "upstream error: %s" % exc})
            except OSError:
                pass
        finally:
            if conn:
                conn.close()
            if waiter:
                self.scheduler.release(waiter, tokens, failed)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def main():
    print("llm-scheduler listening on {}:{} -> {} (concurrency={}, aging={}s)".format(
        HOST, PORT, UPSTREAM, CONCURRENCY, AGING_SEC), flush=True)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
