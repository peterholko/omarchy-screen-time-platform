"""School exemptions require current status from the enrolled root service."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
from school import snapshot


class SchoolStatusTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "status/linnea/school-mode/status.json"
        self.path.parent.mkdir(parents=True)
        self.now = 1800000000
        self.data = {"schemaVersion": 1, "enabled": True, "mode": "school", "updatedAt": self.now}
        self.publish()

    def publish(self, **changes):
        self.path.write_text(json.dumps({**self.data, **changes}))
        os.utime(self.path, (self.now, self.now))

    def read(self, **kwargs):
        return snapshot("linnea", True, self.now, root=self.root, owner=os.getuid(), **kwargs)

    def test_valid_school_and_free_status_and_no_cross_account_exemption(self):
        self.assertTrue(self.read()["active"])
        self.publish(mode="free")
        self.assertEqual(self.read(), {"linked": True, "available": True, "active": False, "reason": "free"})
        self.publish(enabled=False)
        self.assertEqual(self.read()["reason"], "not_enrolled")
        self.assertFalse(snapshot("sibling", True, self.now, root=self.root, owner=os.getuid())["active"])

    def test_off_never_opens_school_files(self):
        with patch("school.os.open", side_effect=AssertionError("should not read")):
            self.assertEqual(snapshot("linnea", False)["reason"], "disabled")

    def test_stopped_service_or_copied_stale_status_has_no_exemption(self):
        for stamp in (self.now - 31, self.now + 6):
            self.publish(updatedAt=stamp)
            self.assertEqual(self.read()["reason"], "stale")
            self.publish()
            os.utime(self.path, (stamp, stamp))
            self.assertEqual(self.read()["reason"], "stale")

    def test_untrusted_permissions_and_wrong_owner_are_refused(self):
        for path in (self.root, self.path.parent.parent.parent, self.path.parent.parent, self.path.parent, self.path):
            mode = path.stat().st_mode & 0o777
            path.chmod(mode | 0o020)
            self.assertEqual(self.read()["reason"], "untrusted")
            path.chmod(mode)
        self.assertFalse(snapshot("linnea", True, self.now, root=self.root, owner=os.getuid() + 1)["active"])

    def test_no_file_directory_or_root_symlinks(self):
        for path in (self.path, self.path.parent, self.path.parent.parent, self.path.parent.parent.parent):
            original = path.with_name(path.name + "-original")
            path.rename(original)
            path.symlink_to(original, target_is_directory=original.is_dir())
            self.assertFalse(self.read()["active"])
            path.unlink(); original.rename(path)
        link = self.root / "linked-root"; link.symlink_to(self.root, target_is_directory=True)
        self.assertFalse(snapshot("linnea", True, self.now, root=link, owner=os.getuid())["active"])

    def test_invalid_or_missing_payload_has_no_exemption(self):
        for change in ({"schemaVersion": True}, {"schemaVersion": 2}, {"enabled": "true"}, {"mode": "unknown"},
                       {"updatedAt": True}, {"updatedAt": float("nan")}, {"updatedAt": float("inf")}):
            self.publish(**change)
            self.assertEqual(self.read()["reason"], "invalid")
        for text in ("not JSON", "[]", "null", "[" * 2000 + "]" * 2000, '{"padding":"' + "x" * 33000 + '"}'):
            self.path.write_text(text)
            self.assertFalse(self.read()["active"])
        self.path.unlink()
        self.assertEqual(self.read()["reason"], "unavailable")
        os.mkfifo(self.path)
        self.assertEqual(self.read()["reason"], "untrusted")

    def test_username_cannot_select_a_path(self):
        for username in ("../linnea", "/linnea", "linnea/../sibling", "linnea\x00", None):
            self.assertEqual(snapshot(username, True, self.now, root=self.root, owner=os.getuid())["reason"], "invalid")


if __name__ == "__main__":
    unittest.main()
