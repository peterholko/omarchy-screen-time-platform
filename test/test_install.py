import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "service/install.py")
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


class InstallTest(unittest.TestCase):
    def test_standalone_school_roster_is_allowed_and_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "etc/omarchy-kids-controls/school-mode.json"
            path.parent.mkdir(parents=True)
            original = '{"users":{"linnea":{"profile":"linnea"}},"profiles":{"linnea":{"school_apps":["org.gnome.Calculator.desktop"]}}}'
            path.write_text(original)
            installer.assert_no_conflict("linnea", root)
            self.assertEqual(path.read_text(), original)
        files = installer.payload(ROOT, "/opt/omarchy")
        self.assertIn(Path("/usr/lib/peterholko-screen-time/service/school.py"), files)

    def test_conflict_is_per_account_and_does_not_change_other_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / installer.CONFLICTS[0].lstrip("/")
            path.parent.mkdir(parents=True); path.write_text('{"users":{"linnea":{}}}')
            installer.assert_no_conflict("sibling", root)
            with self.assertRaisesRegex(ValueError, "already enrolled"):
                installer.assert_no_conflict("linnea", root)
            self.assertEqual(path.read_text(), '{"users":{"linnea":{}}}')

    def test_root_entrypoints_and_sudo_policy_do_not_expose_credits(self):
        files = installer.payload(ROOT, "/opt/Omarchy Kids")
        rule = files[Path("/etc/sudoers.d/peterholko-screen-time")][0].decode()
        self.assertTrue(rule.endswith('-ctl ""\n'))
        self.assertNotIn("-credit", rule)
        ctl = files[Path("/usr/bin/omarchy-peterholko-screen-time-ctl")][0].decode()
        self.assertIn('if (( $# != 0 )); then exit 2; fi', ctl)
        self.assertIn('/usr/bin/env -i PATH=/usr/bin:/bin SUDO_UID="${SUDO_UID:-}"', ctl)
        self.assertIn("python3 -I", ctl)
        self.assertNotIn('"$@"', ctl)
        self.assertEqual(files[installer.ETC / "desktop.env"][0], b'OMARCHY_PATH="/opt/Omarchy Kids"\n')
        for path in files:
            self.assertNotIn("omarchy-kids-controls", str(path))

    def test_upgrade_preserves_local_edits_and_refuses_untracked_collision(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(installer, "safe_target"):
            path = Path(directory) / "wrapper"
            path.write_bytes(b"old")
            files = {path: (b"new", 0o755)}
            with self.assertRaises(ValueError):
                installer.check_existing(files, {}, True)
            tracked = {str(path): installer.digest(b"old")}
            installer.check_existing(files, tracked, True)
            with self.assertRaises(ValueError):
                installer.check_existing(files, tracked, False)
            path.write_bytes(b"local edit")
            with self.assertRaises(ValueError):
                installer.check_existing(files, tracked, True)

    def test_symlinked_install_target_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"; target.write_text("unchanged")
            link = Path(directory) / "link"; link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                installer.safe_target(link)
            self.assertEqual(target.read_text(), "unchanged")

    def test_service_only_writes_its_own_state_and_retains_restart_lock(self):
        service = (ROOT / "service/peterholko-screen-time.service").read_text()
        self.assertIn("RuntimeDirectoryPreserve=restart", service)
        self.assertIn("ReadWritePaths=/var/lib/peterholko-screen-time /run/peterholko-screen-time", service)
        self.assertIn("EnvironmentFile=/etc/peterholko-screen-time/desktop.env", service)
        self.assertNotIn("/usr/share/omarchy", service)


if __name__ == "__main__":
    unittest.main()
