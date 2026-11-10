import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock

from toolservice import tool_service as svc


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        # Runtime is Linux; Windows test runners may not expose AF_UNIX.
        self.unix_family = patch.object(svc.socket, "AF_UNIX", 1, create=True)
        self.unix_family.start()
        self.addCleanup(self.unix_family.stop)

    def broker(self, reply=b'{"ok": true}\n'):
        client = MagicMock()
        client.__enter__.return_value = client
        client.makefile.return_value.__enter__.return_value.readline.return_value = reply
        return client

    def test_sync_runs_once_per_chat_id(self):
        client = self.broker()
        with patch.object(svc, "P4_SYNC_ENABLED", True), patch.object(svc.socket, "socket", return_value=client) as connect:
            token = svc.FILE_SYNC_STATE.set(svc.sync_state_for("chat-1"))
            try:
                svc.ensure_repository_synced()
                svc.ensure_repository_synced()
            finally:
                svc.FILE_SYNC_STATE.reset(token)
            self.assertEqual(connect.call_count, 1)
            token = svc.FILE_SYNC_STATE.set(svc.sync_state_for("chat-1"))
            try:
                svc.ensure_repository_synced()
            finally:
                svc.FILE_SYNC_STATE.reset(token)
            self.assertEqual(connect.call_count, 1)
            token = svc.FILE_SYNC_STATE.set(svc.sync_state_for("chat-2"))
            try:
                svc.ensure_repository_synced()
            finally:
                svc.FILE_SYNC_STATE.reset(token)
            self.assertEqual(connect.call_count, 2)

    def test_calls_without_chat_id_each_sync(self):
        client = self.broker()
        with patch.object(svc, "P4_SYNC_ENABLED", True), patch.object(svc.socket, "socket", return_value=client) as connect:
            for _ in range(2):
                token = svc.FILE_SYNC_STATE.set(svc.sync_state_for(""))
                try:
                    svc.ensure_repository_synced()
                finally:
                    svc.FILE_SYNC_STATE.reset(token)
            self.assertEqual(connect.call_count, 2)

    def test_failed_sync_blocks_search_and_is_not_repeated(self):
        state = svc.FILE_SYNC_STATE.set(svc.sync_state_for("chat-fail"))
        try:
            with patch.object(svc, "P4_SYNC_ENABLED", True), patch.object(svc, "FILE_TOOLS_ENABLED", True), patch.object(svc, "FILE_SEARCH_PATHS", ["/knowledge"]), patch.object(svc.socket, "socket", side_effect=OSError("no broker")) as connect, patch.object(svc.os, "walk") as walk:
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "source was not searched"):
                        svc.tool_search_files({"query": "code"})
                self.assertEqual(connect.call_count, 1)
                walk.assert_not_called()
        finally:
            svc.FILE_SYNC_STATE.reset(state)

    def test_sync_state_cache_is_bounded(self):
        with patch.object(svc, "SYNC_STATE_KEEP", 3):
            for index in range(10):
                svc.sync_state_for("bounded-{}".format(index))
            self.assertLessEqual(len(svc._SYNC_STATES), 3)

    def test_extract_search_terms_strips_particles_and_stopwords(self):
        terms = svc.extract_search_terms("저장소에서 CharacterMovement에서 이동 구현 찾아줘")
        self.assertEqual(terms, ["charactermovement", "이동"])

    def test_score_deprioritizes_deferred_dirs_unless_requested(self):
        project_score = svc.score_file_candidate(
            "src/character/CharacterMovementComponent.cpp", ["charactermovement"], {"charactermovement"}, 10)
        vendor_score = svc.score_file_candidate(
            "vendor/runtime/Private/CharacterMovementComponentAsync.cpp", ["charactermovement"], {"charactermovement"}, 10)
        self.assertGreater(project_score, vendor_score)


class ToolRegistryTests(unittest.TestCase):
    def test_unknown_tool_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown tool"):
            svc.call_tool("nope", {})

    def test_file_tools_are_listed_only_when_enabled(self):
        names = {tool["function"]["name"] for tool in svc.TOOLS}
        if not svc.FILE_TOOLS_ENABLED:
            self.assertFalse(names & {"list_files", "search_files", "read_file_excerpt"})
        self.assertEqual(names, set(svc.TOOL_HANDLERS))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), svc.Handler)
        cls.base = "http://127.0.0.1:{}".format(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body, headers=None):
        request = urllib.request.Request(
            self.base + "/call", data=json.dumps(body).encode(), method="POST",
            headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_tools_and_health(self):
        with urllib.request.urlopen(self.base + "/tools", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["tools"], svc.TOOLS)
        with urllib.request.urlopen(self.base + "/health", timeout=5) as response:
            self.assertTrue(json.loads(response.read())["ok"])

    def test_call_returns_result_and_passes_chat_id(self):
        with patch.object(svc, "run_tool_call", return_value={"x": 1}) as run:
            reply = self.post({"name": "search_web", "arguments": {"query": "q"}}, {"X-Chat-Id": "abc"})
        self.assertEqual(reply, {"ok": True, "result": {"x": 1}})
        self.assertEqual(run.call_args.args[0]["function"]["name"], "search_web")

    def test_tool_failure_is_a_result_not_an_http_error(self):
        reply = self.post({"name": "missing", "arguments": {}})
        self.assertFalse(reply["ok"])
        self.assertIn("Unknown tool", reply["error"])


if __name__ == "__main__":
    unittest.main()
