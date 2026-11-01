import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from astra_harness.application import Application
from astra_harness.contracts import ModelResponse
from astra_harness.process_state import process_has_exited


class ChatModel:
    def complete(self, request):
        return ModelResponse(text="done")


class ProcessStateTests(unittest.TestCase):
    def test_missing_invalid_and_current_process_identity_cannot_be_resolved(self):
        suffix = "a" * 32
        for identity in (None, "legacy", "", 1, "1-invalid", f"0-{suffix}",
                         f"-1-{suffix}", f"{2 ** 40}-{suffix}", f"{os.getpid()}-{suffix}"):
            with self.subTest(identity=identity):
                self.assertFalse(process_has_exited(identity))

    def test_failed_process_query_is_not_exit_evidence(self):
        identity = f"{os.getpid() + 1}-{'a' * 32}"
        for failure in (PermissionError("denied"), OSError("unavailable"), OverflowError()):
            with self.subTest(failure=failure), patch("astra_harness.process_state._pid_has_exited", side_effect=failure):
                self.assertFalse(process_has_exited(identity))

    def test_other_live_process_cannot_resolve_unknown_after_original_application_closes(self):
        script = '''
import sys, threading
from astra_harness.application import Application
from astra_harness.contracts import ModelResponse, ToolCall
from astra_harness.tools import Tool
class Model:
    def complete(self, request):
        return ModelResponse(tool_calls=(ToolCall("late", "write", {}),))
release = threading.Event()
with Application(Model(), tools=(Tool("write", "delayed effect", (), lambda: release.wait(30), side_effect=True),),
                 database=sys.argv[1], limits={"tool_timeout": 0.02}) as app:
    run = app.wait(app.submit("write")["id"])
    assert run["status"] == "unknown"
print(run["id"], flush=True)
sys.stdin.readline()
'''
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "run.sqlite3"
            child = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(database)],
                                     cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                ready = queue.Queue()
                threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
                identifier = ready.get(timeout=10).strip()
                self.assertTrue(identifier, "child did not create an unknown run")
                self.assertIsNone(child.poll())
                with Application(ChatModel(), database=database) as app:
                    state = app.get_run(identifier)
                    self.assertEqual(state["status"], "unknown")
                    self.assertFalse(state["can_resolve_unknown"])
                    with self.assertRaises(RuntimeError):
                        app.resolve_unknown(identifier, "a different host must still wait")
                    with self.assertRaises(RuntimeError):
                        app.submit("must remain blocked")
                    _, error = child.communicate("exit\n", timeout=5)
                    self.assertEqual(child.returncode, 0, error)
                    self.assertTrue(app.get_run(identifier)["can_resolve_unknown"])
                    resolved = app.resolve_unknown(identifier, "original process exited; workspace checked")
                    self.assertEqual(resolved["status"], "unknown")
                    self.assertFalse(resolved["can_resolve_unknown"])
                    self.assertEqual(app.wait(app.submit("continue")["id"])["status"], "completed")
            finally:
                if child.poll() is None:
                    child.communicate("exit\n", timeout=5)

    def test_legacy_unknown_identity_remains_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            with Application(ChatModel(), database=Path(folder) / "legacy.sqlite3", autostart=False) as app:
                run = app.submit("legacy checkpoint")
                run.update(status="unknown", unknown_process="legacy")
                run.pop("execution_process")
                app.store.save(run)
                self.assertFalse(app.get_run(run["id"])["can_resolve_unknown"])
                with self.assertRaises(RuntimeError):
                    app.resolve_unknown(run["id"], "missing identity is not exit evidence")


if __name__ == "__main__":
    unittest.main()
