"""Exercise the actual daemon's jq transition program without a desktop."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "service/time-lib.sh"
LIB_JQ = re.search(r"ST_JQ_LIB <<'JQ'.*?\n(.*?)\nJQ", LIB.read_text(), re.S).group(1)
TICK_JQ = re.search(r"TICK_JQ <<'JQ'.*?\n(.*?)\nJQ", (ROOT / "service/daemon.sh").read_text(), re.S).group(1)
BASH = os.environ.get("TEST_BASH", "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else "/bin/bash")


def jq(program, **arguments):
    args = ["jq", "-nc"]
    for key, value in arguments.items():
        args += ["--argjson", key, json.dumps(value)]
    return json.loads(subprocess.check_output(args + [LIB_JQ + "\n" + program], text=True))


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "service/defaults.json").read_text())
        self.day = jq('new_day("2026-09-10"; 3600; "linnea")')
        self.rt = {"day": self.day["day"], "paused": False, "stretch": 0, "rest_since": None, "nudged": False,
            "lock_after": None, "lock_count": 0, "lock_failures": 0, "last_lock_ok": False, "blocked_since": None}
        self.session = {"present": True, "active": True, "locked": False, "id": "3"}

    def tick(self, **override):
        args = dict(day=self.day, rt=self.rt, profile=self.profile, session=self.session, now=1000,
            step=5, tick=5, rest_reset=300, lock_failures_max=3, moment="12:00", daykey=self.day["day"])
        return jq(TICK_JQ, **{**args, **override})

    def test_custom_profiles_survive_sanitization(self):
        config = jq("default_config")
        config["profiles"]["linnea"] = deepcopy(self.profile)
        config["profiles"]["linnea"]["budget_minutes"]["thu"] = 25
        config["profiles"]["sibling"] = deepcopy(self.profile)
        config["profiles"]["sibling"]["budget_minutes"]["thu"] = 45
        config["users"] = {"linnea": {"profile": "linnea"}, "sibling": {"profile": "sibling"}}
        result = jq('$config | sanitize_config | profile_for("linnea")', config=config)
        self.assertEqual((result["key"], result["profile"]["budget_minutes"]["thu"]), ("linnea", 25))

    def test_default_profiles_match_python(self):
        self.assertEqual(jq("default_profile"), self.profile)

    def test_counts_active_session_and_caps_resume_gap(self):
        self.assertEqual(self.tick()["day"]["spent_seconds"], 5)
        self.assertEqual(self.tick(step=10000)["day"]["spent_seconds"], 20)
        for change in ({"present": False}, {"active": False}, {"locked": True}):
            self.assertEqual(self.tick(session={**self.session, **change})["day"]["spent_seconds"], 0)

    def test_credit_receipts_survive_ticks_and_durable_daemon_write(self):
        self.day.update(credited_seconds=120, spent_seconds=3600, credit_totals={"test.game": 120},
            credit_receipts={"test.game:receipt_123456789": {"credited_seconds": 120}})
        result = self.tick()
        self.assertEqual(result["day"]["spent_seconds"], 3605)
        self.assertEqual(result["day"]["credit_receipts"], self.day["credit_receipts"])
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "PETERHOLKO_SCREEN_TIME_STATE": directory}
            subprocess.run([BASH, "-c", 'source "$1"; st_save_day 1000', "test", str(LIB)],
                input=json.dumps(result["day"]), text=True, env=env, check=True)
            saved = json.loads((Path(directory) / "users/1000/2026-09-10.json").read_text())
            self.assertEqual(saved, result["day"])

    def test_warn_then_grace_lock_then_terminate_failed_shell(self):
        self.day["spent_seconds"] = 3595
        first = self.tick()
        self.assertEqual(first["rt"]["lock_after"], 1060)
        self.assertIn("notify", [a["type"] for a in first["actions"]])
        second = self.tick(day=first["day"], rt=first["rt"], now=1060)
        self.assertIn({"type": "lock", "reason": "empty"}, second["actions"])
        failed = {**first["rt"], "lock_failures": 3}
        self.assertIn({"type": "terminate", "reason": "empty"}, self.tick(day=first["day"], rt=failed, now=1060)["actions"])

    def test_pause_and_agreement_do_not_budget_lock(self):
        self.day["spent_seconds"] = 5000
        self.assertFalse(self.tick(rt={**self.rt, "paused": True})["actions"])
        self.profile["philosophy"] = "together"
        self.assertEqual(self.tick()["day"]["spent_seconds"], 5005)
        self.assertFalse(self.tick()["actions"])

    def test_parent_lock_overrides_agreement_pause_and_notify(self):
        self.profile.update(philosophy="together", on_empty="notify")
        first = self.tick(rt={**self.rt, "paused": True, "parent_lock_requested": True})
        self.assertEqual(first["rt"]["lock_after"], 1000)
        second = self.tick(rt=first["rt"])
        self.assertIn({"type": "lock", "reason": "parent"}, second["actions"])
        locked = self.tick(rt=first["rt"], session={**self.session, "locked": True})
        self.assertFalse(locked["rt"]["parent_lock_requested"])

    def test_bedtime_crosses_midnight(self):
        self.profile["blocked_periods"][0]["enabled"] = True
        for moment in ("20:00", "23:59", "06:59"):
            self.assertEqual(self.tick(moment=moment)["day"]["spent_seconds"], 0)
        self.assertEqual(self.tick(moment="07:00")["day"]["spent_seconds"], 5)

    def test_next_day_clears_pending_budget_lock(self):
        result = self.tick(rt={**self.rt, "day": "2026-09-09", "blocked_since": 1, "lock_after": 2, "lock_count": 3})
        self.assertIsNone(result["rt"]["lock_after"])
        self.assertEqual(result["day"]["spent_seconds"], 5)

    def test_lock_requires_confirmation_not_just_ipc_acceptance(self):
        script = 'source "$1"; sleep() { :; }; st_as_user() { if [[ $4 == "isLocked" ]]; then echo "$CONFIRMED"; fi; }; st_lock_session 1000 3'
        for confirmed, success in (("false", False), ("true", True)):
            result = subprocess.run([BASH, "-c", script, "test", str(LIB)], env={**os.environ, "CONFIRMED": confirmed}, capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, success)

    def test_omarchy_path_with_spaces_crosses_user_environment_intact(self):
        script = 'source "$1"; st_home_of() { echo "/home/test child"; }; st_user_env 1000'
        result = subprocess.check_output([BASH, "-c", script, "test", str(LIB)], env={**os.environ, "OMARCHY_PATH": "/opt/Omarchy Kids"}, text=True)
        self.assertIn("OMARCHY_PATH=/opt/Omarchy Kids\n", result)
        self.assertIn("PATH=/opt/Omarchy Kids/bin:", result)

    def test_published_status_contains_provider_policy_without_credentials(self):
        self.profile["credits"]["providers"] = {"test.game": {"enabled": True, "seconds_per_event": 60, "daily_cap_minutes": 5}}
        runtime = {**self.rt, "session": self.session}
        script = 'source "$1"; st_moment() { echo "12:00"; }; st_status_json 1000 linnea 1000 linnea "$2" "$3" "$4" false'
        result = json.loads(subprocess.check_output([BASH, "-c", script, "test", str(LIB), json.dumps(self.profile), json.dumps(self.day), json.dumps(runtime)], text=True))
        self.assertEqual(result["phase"], "running")
        self.assertEqual(result["plugin_id"], "peterholko.screen-time")
        self.assertEqual(result["credits"]["providers"]["test.game"]["seconds_per_event"], 60)
        self.assertNotIn("authentication", result)
        self.assertNotIn("pin", result)


if __name__ == "__main__":
    unittest.main()
