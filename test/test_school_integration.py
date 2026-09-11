"""Optional contract test against the original School Mode service checkout.

SCHOOL_MODE_SOURCE=/path/to/omarchy-school-mode python3 -m unittest discover -s test -p 'test_school_integration.py' -v
Only fixture services and temporary configuration are used; no system install.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import test_core as platform_cases
import test_daemon as accounting

SOURCE = os.environ.get("SCHOOL_MODE_SOURCE")


@unittest.skipUnless(SOURCE, "set SCHOOL_MODE_SOURCE to check the actual School Mode service")
class SchoolIntegration(unittest.TestCase):
    def setUp(self):
        self.platform = platform_cases.PlatformTest(); self.platform.setUp()
        self.addCleanup(self.platform.doCleanups)
        p = self.platform
        service = str(Path(SOURCE) / "service")
        sys.path.insert(0, service); self.addCleanup(lambda: sys.path.remove(service))
        from omarchy_kids.core import paths
        from omarchy_kids.core.auth import ParentAuth
        from omarchy_kids.core.daemon import Daemon
        env = patch.dict(os.environ, {"SCREEN_TIME_ROOT": str(p.school_root)})
        env.start(); self.addCleanup(env.stop)
        self.host = Daemon(paths.detect(p.school_root), modules=["school"], log=lambda *_: None)
        if hasattr(self.host, "platform"):
            self.addCleanup(self.host.platform.close)
        self.host.clock.now = lambda: p.now
        self.host.auth = ParentAuth(verifier=lambda user, password: password == "school-parent")
        self.assertTrue(self.school("users.set", peer=0, user=p.user, enabled=True)["ok"])
        self.assertTrue(self.school("config.patch", password="school-parent", patch={
            "school_apps": ["org.gnome.Nautilus", "libreoffice-writer"],
            "blocked_periods": [{"label": "Lessons", "enabled": True, "start": "11:00", "end": "13:00", "days": ["thu"], "mode": "free"}]
        })["ok"])
        p.enable_credits()
        self.assertTrue(p.send("config.patch", patch={"respect_school_mode": True})["ok"])

    def school(self, cmd, peer=None, **payload):
        p = self.platform
        result = self.host.dispatch(p.uid if peer is None else peer, {"scope": "school", "cmd": cmd, **payload})
        self.host.refresh(p.now)
        self.school_path = p.school_root / "status" / p.user / "school-mode/status.json"
        if self.school_path.exists():
            os.utime(self.school_path, (p.now, p.now))
        return result

    def tick(self):
        p = self.platform
        config = p.core.config(); user, key, profile = p.core.account(p.uid, config)
        day = p.day(); rt = p.core.runtime(p.uid)
        rt["day"] = day["day"]
        return accounting.jq(accounting.TICK_JQ, day=day, rt=rt, profile=profile, session=rt["session"],
            school=p.core.school(user, profile), now=p.now, step=5, tick=5, rest_reset=300,
            lock_failures_max=3, moment=datetime.fromtimestamp(p.now).strftime("%H:%M"), daykey=day["day"])

    def test_schedule_and_password_protected_free_time_drive_accounting_and_rewards(self):
        p = self.platform
        original_config = self.host.services["school"].path.read_bytes()
        original_public = self.school_path.read_bytes()
        self.assertEqual(self.tick()["day"]["spent_seconds"], 0)
        self.assertEqual(p.award("school-hours")["reason"], "school_mode_active")
        self.assertEqual(self.school_path.read_bytes(), original_public)
        self.assertEqual(self.host.services["school"].path.read_bytes(), original_config)
        self.assertFalse(self.school("mode.set", mode="free")["ok"])
        self.assertTrue(self.school("mode.set", mode="free", password="school-parent")["ok"])
        self.assertEqual(self.tick()["day"]["spent_seconds"], 5)
        self.assertEqual(p.award("school-hours")["credited_seconds"], 0)
        self.assertEqual(p.award("parent-free-time")["credited_seconds"], 120)
        published = json.loads(self.school_path.read_text())
        self.assertEqual(published["schoolApps"], ["org.gnome.Nautilus", "libreoffice-writer"])
        self.assertEqual(self.host.services["school"].path.read_bytes(), original_config)

    def test_chosen_school_and_service_disappearance(self):
        p = self.platform
        p.now = datetime(2026, 9, 10, 14).timestamp()
        self.school("status")
        self.assertEqual(self.tick()["day"]["spent_seconds"], 5)
        self.assertTrue(self.school("mode.set", mode="school")["ok"])
        self.assertEqual(self.tick()["day"]["spent_seconds"], 0)
        p.now += 31
        self.assertEqual(self.tick()["day"]["spent_seconds"], 5)
        self.school("users.set", peer=0, user=p.user, enabled=False)
        self.assertEqual(self.tick()["day"]["spent_seconds"], 5)

    def test_current_game_adapters_honor_school_credit_availability(self):
        if not (Path(SOURCE) / "service/omarchy_kids/core/game_platform.py").exists():
            self.skipTest("game adapter check uses the current community games' shared service")
        from omarchy_kids.core.game_platform import Platform, PROVIDERS
        p = self.platform
        config = p.core.config()
        profile = config["profiles"]["default"]
        for identifier, name, unit in PROVIDERS.values():
            config["providers"][identifier] = {"name": name, "unit": unit}
            profile["credits"]["providers"][identifier] = {"enabled": True, "seconds_per_event": 60, "daily_cap_minutes": 5}
        platform_cases.write(p.core.etc / "config.json", config)
        adapter = Platform(p.core.run, clock=lambda: p.now, trusted_owner=os.getuid())
        self.addCleanup(adapter.close)
        def publish():
            user, key, current = p.core.account(p.uid, config)
            p.core.publish(user, current, p.day(), config)
            os.utime(p.core.run / str(p.uid) / "status.json", (p.now, p.now))
        publish()
        for identifier, _, _ in PROVIDERS.values():
            status = adapter.status(p.uid, identifier)
            self.assertTrue(status["available"])
            self.assertFalse(status["active"])
        self.school("mode.set", mode="free", password="school-parent")
        publish()
        for identifier, _, _ in PROVIDERS.values():
            self.assertTrue(adapter.status(p.uid, identifier)["active"])


if __name__ == "__main__":
    unittest.main()
