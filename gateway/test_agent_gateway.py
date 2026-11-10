import copy
import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from gateway import agent_gateway as gw


def sse(delta=None, finish=None, **extra):
    chunk = {"id": "c1", "object": "chat.completion.chunk", "model": "m",
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    chunk.update(extra)
    return "data: {}\n\n".format(json.dumps(chunk)).encode()


DONE = b"data: [DONE]\n\n"
USAGE = b'data: {"id": "c1", "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 1}}\n\n'
ROLE = sse({"role": "assistant", "content": ""})


def completion(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "c1", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}]}


def tool_call(name, arguments="{}", call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


SEARCH_TOOL = {"type": "function", "function": {"name": "search_files", "description": "d",
                                                 "parameters": {"type": "object", "properties": {}}}}


class FakeUpstream(ThreadingHTTPServer):
    """Stands in for llm-scheduler: scripted replies, records what it was sent."""

    daemon_threads = True

    def __init__(self):
        self.script, self.requests = [], []
        super().__init__(("127.0.0.1", 0), self.make_handler())
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(body) if body else None
                except ValueError:
                    parsed = None
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers.items()), "json": parsed})
                if self.path.startswith("/v1/chat/completions"):
                    reply = outer.script.pop(0)
                elif self.path == "/v1/models":
                    reply = {"status": 200, "json": {"object": "list", "data": [{"id": "m"}]}}
                else:
                    reply = {"status": 200, "json": {"echo": self.path}}
                if "sse" in reply:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event in reply["sse"]:
                        self.wfile.write(event)
                        self.wfile.flush()
                else:
                    raw = json.dumps(reply["json"]).encode()
                    self.send_response(reply.get("status", 200))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            do_GET = do_POST = _handle

        return Handler


class FakeToolService(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tools):
        self.tools, self.calls = tools, []
        self.reply = {"ok": True, "result": {"hit": "src/a.cpp:10"}}
        super().__init__(("127.0.0.1", 0), self.make_handler())
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:{}".format(self.server_address[1])

    def make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload):
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._send({"tools": outer.tools})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append(dict(body, chat_id=self.headers.get("X-Chat-Id")))
                self._send(outer.reply)

        return Handler


class GatewayCase(unittest.TestCase):
    listener = gw.API_LISTENER

    def setUp(self):
        self.upstream = FakeUpstream()
        self.tools = FakeToolService([SEARCH_TOOL])
        registry = gw.ToolRegistry(self.tools.url, 0)
        patches = [
            patch.object(gw, "UPSTREAM_HOST", "127.0.0.1"),
            patch.object(gw, "UPSTREAM_PORT", self.upstream.port),
            patch.object(gw, "UPSTREAM_PREFIX", "/v1"),
            patch.object(gw, "UPSTREAM_ROOT_PREFIX", ""),
            patch.object(gw, "TOOL_SERVICE_URL", self.tools.url),
            patch.object(gw, "REGISTRY", registry),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        handler = type("TestHandler", (gw.Handler,), {"listener": self.listener,
                                                       "log_message": lambda *a, **k: None})
        self.server = gw.Server(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.tools.server_close)
        self.addCleanup(self.tools.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=15)
        payload = None if body is None else json.dumps(body)
        conn.request(method, path, body=payload, headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, raw

    def chat(self, body, headers=None):
        status, raw = self.request("POST", "/v1/chat/completions", body, headers)
        return status, raw

    def chat_upstream(self):
        return [r for r in self.upstream.requests if r["path"] == "/v1/chat/completions"]


class MessageShapingTests(unittest.TestCase):
    def test_systems_merge_without_mutating_history_or_tools(self):
        messages = [
            {"role": "system", "content": "initial"},
            {"role": "user", "content": "question"},
            {"role": "system", "content": "context"},
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "result"},
            {"role": "system", "content": "limit"},
        ]
        original = copy.deepcopy(messages)
        merged = gw.normalize_messages(messages)
        self.assertEqual(merged[0], {"role": "system", "content": "initial\n\ncontext\n\nlimit"})
        self.assertEqual([m["role"] for m in merged], ["system", "user", "assistant", "tool"])
        self.assertEqual(merged[-1]["tool_call_id"], "a")
        self.assertEqual(messages, original)

    def test_structured_system_content_is_preserved(self):
        parts = [{"type": "text", "text": "structured"}]
        self.assertEqual(gw.normalize_messages([{"role": "system", "content": parts}])[0]["content"], parts)

    def test_meta_task_prompt_never_gets_tools(self):
        meta = {"messages": [{"role": "user", "content": "### Task:\nGenerate a title for: 저장소 코드"}]}
        with patch.object(gw, "TOOL_SERVICE_URL", "http://tools"):
            self.assertFalse(gw.tools_wanted(meta, {}))
            self.assertTrue(gw.tools_wanted({"messages": [{"role": "user", "content": "저장소 코드 찾아줘"}]}, {}))

    def test_opt_outs(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        with patch.object(gw, "TOOL_SERVICE_URL", "http://tools"):
            self.assertFalse(gw.tools_wanted(body, {"X-Agent-Tools": "off"}))
            self.assertFalse(gw.tools_wanted(dict(body, tool_choice="none"), {}))
            self.assertFalse(gw.tools_wanted(dict(body, tool_choice={"type": "function"}), {}))
            self.assertTrue(gw.tools_wanted(dict(body, tool_choice="auto"), {}))
        with patch.object(gw, "TOOL_SERVICE_URL", ""):
            self.assertFalse(gw.tools_wanted(body, {}))

    def test_client_tool_wins_name_clash_and_tool_choice_defaults_to_auto(self):
        mine = {"type": "function", "function": {"name": "search_files", "parameters": {}}}
        payload = {"messages": [], "tools": [mine]}
        up = gw.build_upstream_payload(payload, [], gw.API_LISTENER, [SEARCH_TOOL], False, False)
        self.assertEqual(up["tools"], [mine])
        self.assertEqual(up["tool_choice"], "auto")
        self.assertEqual(payload, {"messages": [], "tools": [mine]})

    def test_limit_round_drops_gateway_tools(self):
        up = gw.build_upstream_payload({"messages": []}, [], gw.API_LISTENER, [], True, False)
        self.assertNotIn("tools", up)
        self.assertEqual(up["tool_choice"], "none")

    def test_internal_listener_defaults_thinking_off_but_caller_can_override(self):
        up = gw.build_upstream_payload({"messages": []}, [], gw.INTERNAL_LISTENER, [], False, False)
        self.assertEqual(up["chat_template_kwargs"], {"enable_thinking": False})
        up = gw.build_upstream_payload({"messages": [], "chat_template_kwargs": {"enable_thinking": True}},
                                       [], gw.INTERNAL_LISTENER, [], False, False)
        self.assertEqual(up["chat_template_kwargs"], {"enable_thinking": True})
        up = gw.build_upstream_payload({"messages": []}, [], gw.API_LISTENER, [], False, False)
        self.assertNotIn("chat_template_kwargs", up)

    def test_empty_completion_is_detected(self):
        gw.assert_non_empty(completion("hi"))
        gw.assert_non_empty(completion("", [tool_call("x")]))
        for dead in (completion(""), completion("   "), {"choices": []}, {}):
            with self.assertRaises(gw.EmptyCompletionError):
                gw.assert_non_empty(dead)


class PlainChatTests(GatewayCase):
    def test_answer_without_tool_is_relayed_untouched(self):
        self.upstream.script.append({"json": completion("hello")})
        status, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), completion("hello"))
        sent = self.chat_upstream()[0]["json"]
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["search_files"])
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertEqual(sent["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(self.tools.calls, [])

    def test_tool_call_is_executed_and_the_final_answer_returned(self):
        self.upstream.script += [
            {"json": completion(None, [tool_call("search_files", '{"query": "Config"}')])},
            {"json": completion("it is in src/a.cpp")},
        ]
        status, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "find Config"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "it is in src/a.cpp")
        self.assertEqual(len(self.tools.calls), 1)
        self.assertEqual(self.tools.calls[0]["name"], "search_files")
        self.assertEqual(self.tools.calls[0]["arguments"], {"query": "Config"})
        self.assertTrue(self.tools.calls[0]["chat_id"])
        second = self.chat_upstream()[1]["json"]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "tool"])
        self.assertEqual(second[2]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(second[2]["content"]), {"ok": True, "result": {"hit": "src/a.cpp:10"}})

    def test_one_chat_id_for_all_calls_of_a_request(self):
        self.upstream.script += [
            {"json": completion(None, [tool_call("search_files", "{}", "a")])},
            {"json": completion(None, [tool_call("search_files", "{}", "b")])},
            {"json": completion("done")},
        ]
        self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(len(self.tools.calls), 2)
        self.assertEqual(self.tools.calls[0]["chat_id"], self.tools.calls[1]["chat_id"])

    def test_tool_failure_goes_back_to_the_model(self):
        self.tools.reply = {"ok": False, "error": "Repository sync failed"}
        self.upstream.script += [
            {"json": completion(None, [tool_call("search_files")])},
            {"json": completion("sync failed")},
        ]
        self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        tool_message = self.chat_upstream()[1]["json"]["messages"][-1]
        self.assertEqual(json.loads(tool_message["content"])["error"], "Repository sync failed")

    def test_round_limit_forces_an_answer(self):
        with patch.object(gw, "MAX_TOOL_ROUNDS", 2):
            self.upstream.script += [
                {"json": completion(None, [tool_call("search_files", "{}", "a")])},
                {"json": completion(None, [tool_call("search_files", "{}", "b")])},
                {"json": completion("enough")},
            ]
            status, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "enough")
        last = self.chat_upstream()[2]["json"]
        self.assertNotIn("tools", last)
        self.assertEqual(last["tool_choice"], "none")
        self.assertIn("Tool round limit reached", last["messages"][0]["content"])

    def test_client_declared_tool_is_returned_not_executed(self):
        reply = completion(None, [tool_call("my_tool", '{"a": 1}')])
        self.upstream.script.append({"json": reply})
        mine = {"type": "function", "function": {"name": "my_tool", "parameters": {}}}
        status, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": [mine]})
        self.assertEqual(json.loads(raw), reply)
        self.assertEqual(self.tools.calls, [])
        names = [t["function"]["name"] for t in self.chat_upstream()[0]["json"]["tools"]]
        self.assertEqual(names, ["my_tool", "search_files"])

    def test_opt_out_header_sends_no_tools(self):
        self.upstream.script.append({"json": completion("hi")})
        self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]}, {"X-Agent-Tools": "off"})
        sent = self.chat_upstream()[0]
        self.assertNotIn("tools", sent["json"])
        self.assertNotIn("X-Agent-Tools", sent["headers"])

    def test_upstream_error_status_and_body_are_relayed(self):
        self.upstream.script.append({"status": 400, "json": {"error": "bad template"}})
        status, raw = self.chat({"model": "m", "messages": []})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw), {"error": "bad template"})

    def test_empty_completion_is_a_502(self):
        self.upstream.script.append({"json": completion("")})
        status, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 502)
        self.assertIn("empty completion", json.loads(raw)["error"])

    def test_invalid_json_is_a_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("POST", "/v1/chat/completions", body=b"{nope", headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)

    def test_tool_service_down_still_answers_without_tools(self):
        self.tools.shutdown()
        self.tools.server_close()
        self.upstream.script.append({"json": completion("plain")})
        status, raw = self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        self.assertNotIn("tools", self.chat_upstream()[0]["json"])


class StreamChatTests(GatewayCase):
    def stream(self, body=None, headers=None):
        body = dict(body or {"messages": [{"role": "user", "content": "x"}]}, model="m", stream=True)
        status, raw = self.chat(body, headers)
        return status, raw

    def test_text_is_streamed_through(self):
        self.upstream.script.append({"sse": [ROLE, sse({"content": "He"}), sse({"content": "llo"}),
                                             sse({}, "stop"), DONE]})
        status, raw = self.stream()
        self.assertEqual(status, 200)
        self.assertEqual(raw, ROLE + sse({"content": "He"}) + sse({"content": "llo"}) + sse({}, "stop") + DONE)
        self.assertTrue(self.chat_upstream()[0]["json"]["stream"])

    def test_tool_round_is_invisible_to_the_caller(self):
        first = [
            ROLE,
            sse({"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                 "function": {"name": "search_files", "arguments": ""}}]}),
            sse({"tool_calls": [{"index": 0, "function": {"arguments": '{"query":'}}]}),
            sse({"tool_calls": [{"index": 0, "function": {"arguments": ' "Config"}'}}]}),
            sse({}, "tool_calls"),
            USAGE,
            DONE,
        ]
        second = [ROLE, sse({"content": "found it"}), sse({}, "stop"), DONE]
        self.upstream.script += [{"sse": first}, {"sse": second}]
        status, raw = self.stream()
        self.assertEqual(status, 200)
        self.assertEqual(raw, ROLE + sse({"content": "found it"}) + sse({}, "stop") + DONE)
        self.assertNotIn(b"tool_calls", raw)
        self.assertEqual(self.tools.calls[0]["arguments"], {"query": "Config"})
        messages = self.chat_upstream()[1]["json"]["messages"]
        self.assertEqual(messages[1]["tool_calls"][0]["function"],
                         {"name": "search_files", "arguments": '{"query": "Config"}'})
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call_9")

    def test_text_before_a_tool_call_is_kept_and_the_answer_continues_the_stream(self):
        first = [ROLE, sse({"content": "Let me check. "}),
                 sse({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "search_files", "arguments": "{}"}}]}),
                 sse({}, "tool_calls"), DONE]
        second = [ROLE, sse({"content": "Done."}), sse({}, "stop"), DONE]
        self.upstream.script += [{"sse": first}, {"sse": second}]
        status, raw = self.stream()
        # An SSE comment keeps the connection alive while the tool runs; clients ignore it.
        self.assertEqual(raw, ROLE + sse({"content": "Let me check. "}) + b": running tools\n\n"
                         + sse({"content": "Done."}) + sse({}, "stop") + DONE)
        self.assertEqual(self.chat_upstream()[1]["json"]["messages"][1]["content"], "Let me check. ")

    def test_client_tool_call_is_streamed_back_to_the_caller(self):
        events = [ROLE,
                  sse({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "my_tool", "arguments": "{}"}}]}),
                  sse({}, "tool_calls"), DONE]
        self.upstream.script.append({"sse": events})
        mine = {"type": "function", "function": {"name": "my_tool", "parameters": {}}}
        status, raw = self.stream({"messages": [{"role": "user", "content": "x"}], "tools": [mine]})
        self.assertEqual(raw, b"".join(events))
        self.assertEqual(self.tools.calls, [])

    def test_empty_stream_is_a_502(self):
        self.upstream.script.append({"sse": [ROLE, sse({}, "stop"), DONE]})
        status, raw = self.stream()
        self.assertEqual(status, 502)
        self.assertIn("empty completion", json.loads(raw)["error"])

    def test_upstream_error_before_the_stream_is_relayed(self):
        self.upstream.script.append({"status": 400, "json": {"error": "nope"}})
        status, raw = self.stream()
        self.assertEqual(status, 400)


class RoutingTests(GatewayCase):
    def test_other_paths_are_relayed_to_the_scheduler(self):
        status, raw = self.request("GET", "/v1/scheduler/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"echo": "/v1/scheduler/status"})

    def test_models_are_relayed(self):
        status, raw = self.request("GET", "/v1/models")
        self.assertEqual(json.loads(raw)["data"][0]["id"], "m")

    def test_models_fall_back_when_upstream_is_down(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        status, raw = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertIn("agent_gateway_warning", json.loads(raw))

    def test_health_reports_tools(self):
        status, raw = self.request("GET", "/health")
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["tools"], ["search_files"])

    def test_api_listener_forwards_caller_headers_and_the_real_client_address(self):
        self.upstream.script.append({"json": completion("hi")})
        self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]},
                  {"X-LLM-Priority": "P1", "X-Caller": "wiki-relay", "X-Forwarded-For": "6.6.6.6"})
        headers = self.chat_upstream()[0]["headers"]
        self.assertEqual(headers["X-LLM-Priority"], "P1")
        self.assertEqual(headers["X-Caller"], "wiki-relay")
        self.assertEqual(headers["X-Forwarded-For"], "127.0.0.1")

    def test_api_listener_adds_no_identity_of_its_own(self):
        self.upstream.script.append({"json": completion("hi")})
        self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        headers = self.chat_upstream()[0]["headers"]
        self.assertNotIn("X-LLM-Priority", headers)
        self.assertNotIn("X-Caller", headers)


class InternalListenerTests(GatewayCase):
    listener = gw.INTERNAL_LISTENER

    def test_open_webui_gets_fixed_priority_and_thinking_off(self):
        self.upstream.script.append({"json": completion("hi")})
        self.chat({"model": "m", "messages": [{"role": "user", "content": "x"}]}, {"X-LLM-Priority": "P2"})
        sent = self.chat_upstream()[0]
        self.assertEqual(sent["headers"]["X-LLM-Priority"], gw.LLM_PRIORITY)
        self.assertEqual(sent["headers"]["X-Caller"], gw.LLM_CALLER)
        self.assertEqual(sent["json"]["chat_template_kwargs"], {"enable_thinking": False})


class ClientGoneTests(unittest.TestCase):
    def test_closed_peer_is_detected(self):
        import socket
        a, b = socket.socketpair()
        self.assertFalse(gw.client_closed(a))
        b.close()
        self.assertTrue(gw.client_closed(a))
        a.close()


if __name__ == "__main__":
    unittest.main()
