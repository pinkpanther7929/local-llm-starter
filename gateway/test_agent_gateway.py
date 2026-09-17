import copy
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from gateway import agent_gateway as gateway


class ChatCompatibilityTests(unittest.TestCase):
    def test_systems_merge_without_mutating_history_or_tools(self):
        payload = {"messages": [
            {"role": "system", "content": "initial"},
            {"role": "user", "content": "question"},
            {"role": "system", "content": "context"},
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "result"},
            {"role": "system", "content": "limit"},
        ]}
        original = copy.deepcopy(payload)
        with patch.object(gateway, "request_json") as request:
            gateway.request_chat(payload)
        messages = request.call_args.args[1]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "initial\n\ncontext\n\nlimit"})
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool"])
        self.assertEqual(messages[-1]["tool_call_id"], "a")
        self.assertEqual(payload, original)

    def test_structured_system_content_is_preserved(self):
        parts = [{"type": "text", "text": "structured"}]
        with patch.object(gateway, "request_json") as request:
            gateway.request_chat({"messages": [{"role": "system", "content": parts}]})
        self.assertEqual(request.call_args.args[1]["messages"][0]["content"], parts)

    def test_bad_request_is_not_retried(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("http://test", 400, "invalid", {}, None)
        with patch.object(gateway.urllib.request, "build_opener", return_value=opener), patch.object(gateway.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                gateway.request_json("http://test", {})
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_chat_uses_long_timeout_without_retries(self):
        with patch.object(gateway, "request_json", return_value={}) as request:
            gateway.request_chat({"messages": []})
        self.assertEqual(request.call_args.kwargs,
                         {"timeout": gateway.LLM_TIMEOUT_SECONDS, "retries": 0})

    def test_chat_timeout_is_not_retried(self):
        for error in (TimeoutError("slow generation"),
                      urllib.error.URLError(TimeoutError("slow generation"))):
            with self.subTest(error=type(error).__name__):
                opener = MagicMock()
                opener.open.side_effect = error
                with patch.object(gateway.urllib.request, "build_opener", return_value=opener), patch.object(gateway.time, "sleep") as sleep:
                    with self.assertRaises(type(error)):
                        gateway.request_chat({"messages": []})
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(opener.open.call_args.kwargs["timeout"], gateway.LLM_TIMEOUT_SECONDS)
                sleep.assert_not_called()

    def test_non_chat_requests_keep_short_timeout_and_retries(self):
        opener = MagicMock()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        opener.open.side_effect = [TimeoutError("temporary"), response]
        with patch.object(gateway.urllib.request, "build_opener", return_value=opener), patch.object(gateway.time, "sleep") as sleep:
            self.assertEqual(gateway.request_json("http://test/models"), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], gateway.HTTP_TIMEOUT_SECONDS)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
