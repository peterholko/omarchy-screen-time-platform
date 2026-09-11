import concurrent.futures
from copy import deepcopy
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import pwd
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
from core import Core, DEFAULT_PROFILE, read, write


class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="screen-time-platform-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.now = datetime(2026, 9, 10, 12).timestamp()
        self.core = Core(root / "etc", root / "state", root / "run", clock=lambda: self.now,
            verifier=lambda password: password == " correct parent password ")
        self.uid = os.getuid() or pwd.getpwnam("nobody").pw_uid
        self.user = pwd.getpwuid(self.uid).pw_name
        config = self.core.config()
        config["users"][self.user] = {"profile": "default"}
        config["providers"] = {"test.garden": {"name": "Garden", "unit": "round"}, "test.hotel": {"name": "Hotel", "unit": "problem"}}
        write(self.core.etc / "config.json", config)
        self.rtpath = self.core.run / str(self.uid) / "runtime.json"
        write(self.rtpath, {"paused": False, "updated_at": self.now, "session": {"present": True, "active": True, "locked": False}})

    def send(self, command, **payload):
        return self.core.request(self.uid, {"cmd": command, "credential": " correct parent password ", **payload})

    def enable_credits(self, cap=5, provider_cap=4, seconds=120):
        result = self.send("config.patch", patch={"credits": {"enabled": True, "daily_cap_minutes": cap,
            "providers": {key: {"enabled": True, "seconds_per_event": seconds, "daily_cap_minutes": provider_cap} for key in ("test.garden", "test.hotel")}}})
        self.assertTrue(result["ok"], result)

    def award(self, suffix="1", provider="test.garden", day=None):
        return self.core.award(self.user, provider, "completion_event_" + suffix, day or datetime.fromtimestamp(self.now).date().isoformat())

    def day(self):
        config = self.core.config()
        user, key, profile = self.core.account(self.uid, config)
        return self.core.day(user, key, profile)[1]

    def test_default_password_no_questions_and_no_public_credit_command(self):
        self.assertFalse(self.core.config()["authentication"]["pin_enabled"])
        self.assertFalse(self.send("config.get", credential="wrong")["ok"])
        self.assertTrue(self.send("config.get")["ok"])
        self.assertEqual(self.send("config.get", auth_method="pin", credential="1234")["error"], "pin_disabled")
        for command in ("quiz", "quiz.next", "answer", "award", "credit", "provider-register"):
            self.assertEqual(self.send(command)["error"], "unknown_command")
        self.assertNotIn("earn", self.send("config.get")["profile"])

    def test_agreement_settings_still_need_authentication(self):
        self.assertTrue(self.send("config.patch", patch={"philosophy": "together"})["ok"])
        for command, data in [("config.get", {}), ("config.patch", {"patch": {"agreement_minutes": 500}}), ("pin.set", {"enabled": True, "new_pin": "2468"}), ("grant", {"minutes": 60})]:
            self.assertFalse(self.send(command, credential="", **data)["ok"])
        self.assertTrue(self.send("reflect", credential="", text="I chose to take a break.")["ok"])

    def test_pin_enable_change_disable_and_password_recovery(self):
        self.assertTrue(self.send("pin.set", enabled=True, new_pin="2468")["ok"])
        saved = (self.core.etc / "config.json").read_text()
        self.assertNotIn('"2468"', saved)
        self.assertTrue(self.send("config.get", auth_method="pin", credential="2468")["ok"])
        self.assertTrue(self.send("config.get")["ok"])
        self.assertTrue(self.send("pin.set", auth_method="pin", credential="2468", enabled=True, new_pin="987654")["ok"])
        self.assertEqual(self.send("config.get", auth_method="pin", credential="2468")["error"], "bad_pin")
        self.assertTrue(self.send("config.get", auth_method="pin", credential="987654")["ok"])
        self.assertTrue(self.send("pin.set", enabled=False)["ok"])
        self.assertEqual(self.send("config.get", auth_method="pin", credential="987654")["error"], "pin_disabled")
        self.assertTrue(self.send("config.get")["ok"])
        self.assertIsNone(self.core.config()["authentication"]["pin"])

    def test_pin_cannot_be_enabled_by_config_patch_or_bad_password(self):
        for patch_value in ({"authentication": {"pin_enabled": True}}, {"pin": "2468"}, {"earn": {"enabled": True}}, {"credits": {"providers": {"made.up": {}}}}):
            self.assertEqual(self.send("config.patch", patch=patch_value)["error"], "invalid_patch")
        self.assertEqual(self.send("pin.set", enabled=True, new_pin="2468", credential="child password")["error"], "bad_password")
        self.assertFalse(self.core.config()["authentication"]["pin_enabled"])

    def test_limits_validation_and_settings_keep_daily_totals(self):
        for patch_value in ({"budget_minutes": {"mon": -1}}, {"budget_minutes": {"tue": True}}, {"blocked_periods": [{"label": "Bad", "enabled": True, "start": "25:00", "end": "07:00"}]}, {"credits": {"enabled": 1}}, {"credits": {"daily_cap_minutes": -1}}):
            self.assertEqual(self.send("config.patch", patch=patch_value)["error"], "invalid_patch")
        self.assertTrue(self.send("grant", minutes=15)["ok"])
        self.enable_credits(); self.award()
        self.assertTrue(self.send("config.patch", patch={"budget_minutes": {"thu": 75}})["ok"])
        self.assertEqual(self.day()["granted_seconds"], 900)
        self.assertEqual(self.day()["credited_seconds"], 120)
        self.assertEqual(self.day()["budget_seconds"], 4500)

    def test_concurrent_password_attempts_are_rate_limited_without_stalling_state(self):
        started, release = threading.Event(), threading.Event()
        def verify(password):
            started.set(); release.wait(2); return False
        self.core.verifier = verify
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.send, "config.get")
            self.assertTrue(started.wait(1))
            self.assertEqual(self.send("config.get")["error"], "password_checking")
            with self.core.locked():
                pass
            release.set(); self.assertEqual(first.result()["error"], "bad_password")
        self.core.verifier = lambda password: False
        self.send("config.get"); self.send("config.get")
        self.assertEqual(self.send("config.get")["error"], "password_locked_out")

    def test_credit_caps_for_each_provider_and_all_games(self):
        self.enable_credits(cap=5, provider_cap=3)
        self.assertEqual([self.award(str(i))["credited_seconds"] for i in range(3)], [120, 60, 0])
        self.assertEqual(self.award("hotel", "test.hotel")["credited_seconds"], 120)
        self.assertEqual(self.award("hotel2", "test.hotel")["credited_seconds"], 0)
        self.assertEqual(self.day()["credited_seconds"], 300)
        self.assertEqual(self.day()["credit_totals"], {"test.garden": 180, "test.hotel": 120})

    def test_concurrent_retries_and_service_restart_credit_once(self):
        self.enable_credits()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.award(), range(8)))
        self.assertTrue(all(result["credited_seconds"] == 120 for result in results))
        self.assertEqual(sum(not result["already_credited"] for result in results), 1)
        self.core = Core(self.core.etc, self.core.state, self.core.run, clock=lambda: self.now)
        self.assertTrue(self.award()["already_credited"])
        self.assertEqual(self.day()["credited_seconds"], 120)

    def test_next_day_rejects_yesterdays_receipt_and_resets_caps(self):
        self.enable_credits(cap=2); self.award()
        self.now += 86400
        runtime = read(self.rtpath, {})
        runtime["updated_at"] = self.now
        write(self.rtpath, runtime)
        self.assertEqual(self.award(day="2026-09-10")["error"], "wrong_day")
        self.assertEqual(self.award("new")["credited_seconds"], 120)

    def test_stale_session_cannot_credit_and_does_not_become_retroactive(self):
        self.enable_credits()
        self.now += 31
        self.assertEqual(self.award()["reason"], "session_unavailable")
        runtime = read(self.rtpath, {})
        runtime["updated_at"] = self.now
        write(self.rtpath, runtime)
        self.assertEqual(self.award()["credited_seconds"], 0)
        self.assertEqual(self.award("fresh")["credited_seconds"], 120)

    def test_disabled_or_blocked_credit_never_becomes_retroactive(self):
        self.assertEqual(self.award()["credited_seconds"], 0)
        self.enable_credits()
        self.assertTrue(self.award()["already_credited"])
        self.assertEqual(self.award()["credited_seconds"], 0)
        for index, patch_value in enumerate(({"philosophy": "together"}, {"philosophy": "limits", "blocked_periods": [{"label": "Lunch", "enabled": True, "start": "11:00", "end": "13:00"}]})):
            self.assertTrue(self.send("config.patch", patch=patch_value)["ok"])
            self.assertEqual(self.award(str(index))["credited_seconds"], 0)
        self.send("config.patch", patch={"blocked_periods": []})
        rt = read(self.rtpath, {}); rt["session"]["locked"] = True; write(self.rtpath, rt)
        self.assertEqual(self.award("locked")["reason"], "session_unavailable")
        rt["session"]["locked"] = False; rt["paused"] = True; write(self.rtpath, rt)
        self.assertEqual(self.award("paused")["reason"], "policy_blocked")

    def test_failed_credit_save_and_failed_publication_can_be_retried(self):
        self.enable_credits()
        with patch("core.write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.award()
        self.assertEqual(self.day()["credited_seconds"], 0)
        with patch.object(self.core, "publish", side_effect=OSError("status unavailable")):
            with self.assertRaises(OSError):
                self.award()
        self.assertTrue(self.award()["already_credited"])
        self.assertEqual(self.day()["credited_seconds"], 120)

    def test_provider_and_receipt_validation(self):
        self.assertEqual(self.award(provider="unknown.game")["error"], "unregistered_provider")
        self.assertEqual(self.award(provider="../../escape")["error"], "invalid_provider")
        self.assertEqual(self.core.award(self.user, "test.garden", "bad/id", "2026-09-10")["error"], "invalid_receipt")
        self.assertEqual(self.send("grant", minutes=True)["error"], "bad_minutes")
        self.assertEqual(self.send("grant", minutes=601)["error"], "bad_minutes")

    def test_request_cannot_select_another_account(self):
        self.assertTrue(self.send("grant", user="root", minutes=10)["ok"])
        self.assertEqual(self.day()["granted_seconds"], 600)
        self.assertEqual(self.core.request(0, {"cmd": "config.get"})["error"], "not_managed")

    def test_parent_lock_and_pause_requests_keep_budget(self):
        self.send("pause")
        self.assertTrue(read(self.rtpath, {})["paused"])
        self.send("resume"); self.send("lock")
        self.assertTrue(read(self.rtpath, {})["parent_lock_requested"])
        self.assertEqual(self.day()["spent_seconds"], 0)

    def test_private_secrets_and_public_read_only_status(self):
        self.send("pin.set", enabled=True, new_pin="2468")
        self.send("grant", minutes=5)
        self.assertEqual((self.core.etc / "config.json").stat().st_mode & 0o777, 0o600)
        status = self.core.run / str(self.uid) / "status.json"
        self.assertEqual(status.stat().st_mode & 0o777, 0o640)
        self.assertNotIn("scrypt", status.read_text())
        self.assertNotIn("correct parent password", status.read_text())


if __name__ == "__main__":
    unittest.main()
