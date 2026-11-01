"""Existing HTTP approval surface for the new edit adapter, using real local files."""
import hashlib
import json
import unittest

from tests.test_workspace_host import WorkspaceHostFixture
from tests import test_workspace_web as workspace_web


class CoreToolsWebTests(WorkspaceHostFixture, unittest.TestCase):
    server = workspace_web.WorkspaceWebTests.server
    request = workspace_web.WorkspaceWebTests.request

    def test_edit_preview_and_approval_remain_bound_after_http_workspace_switch(self):
        app, services, _ = self.build()
        self.port = self.server(app, services)
        arguments = {"path": "same.txt", "old_text": "startup-content", "new_text": "edited through approval",
                     "expected_sha256": hashlib.sha256((self.startup / "same.txt").read_bytes()).hexdigest()}
        waiting = app.wait(app.submit_tool("files.edit", arguments)["id"])
        self.assertEqual(waiting["status"], "waiting_approval")
        status, spec = self.request("/api/workspaces/validate", "POST", {"path": str(self.other)})
        self.assertEqual(status, 200)
        revision = self.request("/api/workspaces")[1]["revision"]
        self.assertEqual(self.request("/api/workspaces/select", "POST", {"path": spec["path"],
            "expected_identity": spec["identity"], "expected_revision": revision})[0], 200)
        status, preview = self.request(f'/api/runs/{waiting["id"]}/preview')
        self.assertEqual(status, 200, preview)
        text = json.dumps(preview)
        self.assertIn("startup-content", text)
        self.assertIn("edited through approval", text)
        self.assertNotIn("other-content", text)
        self.assertFalse((self.data / "file-history").exists())
        status, _ = self.request(f'/api/runs/{waiting["id"]}/approve', "POST", {"approved": True})
        self.assertEqual(status, 200)
        self.assertEqual(app.wait(waiting["id"])["status"], "completed")
        self.assertEqual((self.startup / "same.txt").read_text(), "edited through approval")
        self.assertEqual((self.other / "same.txt").read_text(), "other-content")


if __name__ == "__main__":
    unittest.main()
