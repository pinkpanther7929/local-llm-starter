import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("broker", Path(__file__).with_name("p4-sync-service.py"))
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class SyncSafetyTests(unittest.TestCase):
    def client_spec(self, options="noallwrite noclobber", root=None):
        return "info: Root: {}\ninfo: Options: {}\n".format(root or broker.WORKSPACE, options)

    def test_success_only_runs_fixed_commands(self):
        with patch.object(broker, "run_p4", side_effect=[self.client_spec(), "valid", "info: File(s) not opened on this client.", "synced"]) as run:
            self.assertEqual(broker.sync_workspace(), {"ok": True})
        self.assertEqual([c.args for c in run.call_args_list], [("client", "-o"), ("login", "-s"), ("opened",), ("sync", "-s")])

    def test_unsafe_workspace_refused(self):
        for options in ("allwrite noclobber", "noallwrite clobber"):
            with patch.object(broker, "run_p4", return_value=self.client_spec(options)) as run:
                with self.assertRaises(RuntimeError):
                    broker.sync_workspace()
                self.assertEqual(run.call_count, 1)

    def test_wrong_root_refused(self):
        with patch.object(broker, "run_p4", return_value=self.client_spec(root="/wrong/root")):
            with self.assertRaisesRegex(RuntimeError, "Root"):
                broker.sync_workspace()

    def test_open_edits_refused(self):
        with patch.object(broker, "run_p4", side_effect=[self.client_spec(), "valid", "info: //depot/source.cpp#1 - edit default change"]) as run:
            with self.assertRaisesRegex(RuntimeError, "opened files"):
                broker.sync_workspace()
        self.assertEqual(run.call_count, 3)

    def test_sync_error_propagates(self):
        with patch.object(broker, "run_p4", side_effect=[self.client_spec(), "valid", "", RuntimeError("sync conflict")]):
            with self.assertRaisesRegex(RuntimeError, "sync conflict"):
                broker.sync_workspace()


if __name__ == "__main__":
    unittest.main()
