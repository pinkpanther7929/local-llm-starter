import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scheduler import llm_scheduler as sched


class FakeUpstream(BaseHTTPRequestHandler):
    order = []
    seen_headers = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "fake"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeUpstream.order.append(payload.get("tag"))
        FakeUpstream.seen_headers.append(dict(self.headers))
        time.sleep(payload.get("delay", 0))
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part in ("a", "b"):
                self.wfile.write(('data: {"delta":"%s"}\n\n' % part).encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b'data: {"usage":{"completion_tokens":7}}\n\ndata: [DONE]\n\n')
            return
        body = json.dumps({"tag": payload.get("tag"), "usage": {"completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def post(port, payload, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/v1/chat/completions", json.dumps(payload),
                 dict({"Content-Type": "application/json"}, **(headers or {})))
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        FakeUpstream.order, FakeUpstream.seen_headers = [], []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        up_port = start(self.upstream)
        self.scheduler = sched.Scheduler(concurrency=1, aging_sec=0)
        patches = [patch.object(sched, "UPSTREAM", "http://127.0.0.1:%d" % up_port),
                   patch.object(sched.Handler, "scheduler", self.scheduler)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.proxy = sched.Server(("127.0.0.1", 0), sched.Handler)
        self.port = start(self.proxy)
        self.addCleanup(self.proxy.shutdown)
        self.addCleanup(self.upstream.shutdown)

    def test_ip_rule_applies_to_forwarded_address(self):
        with patch.object(sched, "TRUST_FORWARDED", True), patch.object(sched, "IP_PRIORITY", {"10.6.5.155": 1}):
            post(self.port, {"tag": "fwd"}, {"X-Forwarded-For": "10.6.5.155"})
        caller = self.scheduler.status()["callers"]["10.6.5.155"]
        self.assertEqual(caller["by_priority"]["P1"], 1)

    def test_passthrough_get_models(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        self.assertEqual(json.loads(resp.read())["data"][0]["id"], "fake")

    def test_p0_runs_before_waiting_p2(self):
        results = []

        def call(tag, prio, delay=0.0):
            results.append(post(self.port, {"tag": tag, "delay": delay},
                                {"X-LLM-Priority": prio, "X-Caller": tag}))

        first = threading.Thread(target=call, args=("first", "P2", 0.5))
        first.start()
        time.sleep(0.15)
        later = [threading.Thread(target=call, args=("batch", "P2")),
                 threading.Thread(target=call, args=("human", "P0"))]
        later[0].start()
        time.sleep(0.05)
        later[1].start()
        for t in [first] + later:
            t.join(10)
        self.assertEqual(FakeUpstream.order, ["first", "human", "batch"])
        self.assertTrue(all(status == 200 for status, _ in results))

    def test_headers_forwarded_and_body_unchanged(self):
        status, data = post(self.port, {"tag": "x"}, {"X-LLM-Priority": "P1", "X-Caller": "t"})
        self.assertEqual((status, json.loads(data)["tag"]), (200, "x"))
        self.assertEqual(FakeUpstream.seen_headers[0].get("X-Caller"), "t")

    def test_streaming_is_relayed_and_tokens_counted(self):
        status, data = post(self.port, {"tag": "s", "stream": True}, {"X-Caller": "streamer"})
        self.assertEqual(status, 200)
        self.assertIn(b"[DONE]", data)
        stat = self.scheduler.status()["callers"]["streamer"]
        self.assertEqual(stat["completion_tokens"], 7)
        self.assertEqual(self.scheduler.status()["running"], [])

    def test_status_reports_callers(self):
        post(self.port, {"tag": "a"}, {"X-LLM-Priority": "P1", "X-Caller": "wiki-relay"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/v1/scheduler/status")
        status = json.loads(conn.getresponse().read())
        self.assertEqual(status["callers"]["wiki-relay"]["by_priority"]["P1"], 1)
        self.assertEqual(status["concurrency"], 1)

    def test_upstream_down_returns_502_and_frees_slot(self):
        with patch.object(sched, "UPSTREAM", "http://127.0.0.1:1"):
            status, _ = post(self.port, {"tag": "x"})
        self.assertEqual(status, 502)
        self.assertEqual(self.scheduler.status()["running"], [])


class PriorityTests(unittest.TestCase):
    def test_parse_priority(self):
        self.assertEqual([sched.parse_priority(v) for v in ("P0", "p1", "2", "P9", "", None)],
                         [0, 1, 2, None, None, None])

    def test_header_beats_ip_and_default(self):
        with patch.object(sched, "IP_PRIORITY", {"10.6.5.155": 1}):
            self.assertEqual(sched.resolve_priority("P0", "10.6.5.155"), 0)
            self.assertEqual(sched.resolve_priority(None, "10.6.5.155"), 1)
            self.assertEqual(sched.resolve_priority(None, "1.2.3.4"), 2)

    def test_forwarded_address_is_used_only_when_trusted(self):
        headers = {"X-Forwarded-For": "10.6.5.155, 172.18.0.5"}
        with patch.object(sched, "TRUST_FORWARDED", False):
            self.assertEqual(sched.client_ip(headers, "172.18.0.9"), "172.18.0.9")
        with patch.object(sched, "TRUST_FORWARDED", True):
            self.assertEqual(sched.client_ip(headers, "172.18.0.9"), "10.6.5.155")
            self.assertEqual(sched.client_ip({}, "172.18.0.9"), "172.18.0.9")

    def test_aging_promotes_waiters(self):
        waiter = sched.Waiter(2, "x", 1)
        self.assertEqual(waiter.effective(waiter.enqueued + 130, 60), 0)
        self.assertEqual(waiter.effective(waiter.enqueued + 130, 0), 2)

    def test_cancelled_waiter_leaves_queue(self):
        s = sched.Scheduler(concurrency=1, aging_sec=0)
        running = s.enqueue(1, "a")
        waiting = s.enqueue(0, "b")
        s.cancel(waiting)
        self.assertEqual(s.status()["queue"], [])
        s.release(running)
        self.assertEqual(s.status()["running"], [])


if __name__ == "__main__":
    unittest.main()
