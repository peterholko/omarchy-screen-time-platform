"""Screen-time control and credit API. No questions, scoring or game code.

Only installed root commands instantiate this class in production. Tests
inject a temporary layout, a clock and a verifier; production accepts no
environment override for any of those dependencies.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import pwd
import re
import tempfile
import time
from auth import parent_password_ok, pin_record, verify_pin
from school import snapshot as school_snapshot

PLUGIN_ID = "peterholko.screen-time"
API_VERSION = 1
PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+")
DEFAULT_PROFILE = json.loads(Path(__file__).with_name("defaults.json").read_text())
DEFAULT_RUNTIME = {"paused": False, "stretch": 0, "rest_since": None, "nudged": False,
    "lock_after": None, "lock_count": 0, "lock_failures": 0, "last_lock_ok": False,
    "blocked_since": None, "session": {"present": False, "active": False, "locked": False, "id": None}}


class Refusal(Exception):
    def __init__(self, error, **details):
        self.result = {"ok": False, "error": error, **details}


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def read(path, default):
    if path.is_symlink():
        raise Refusal("unsafe_path")
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return deepcopy(default)


def write(path, value, mode=0o600, gid=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if gid is not None and os.geteuid() == 0:
            os.fchown(fd, 0, gid)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, separators=(",", ":")); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Core:
    def __init__(self, etc=Path("/etc/peterholko-screen-time"), state=Path("/var/lib/peterholko-screen-time"),
                 run=Path("/run/peterholko-screen-time"), clock=time.time, verifier=parent_password_ok,
                 school_reader=school_snapshot):
        self.etc, self.state, self.run = Path(etc), Path(state), Path(run)
        self.clock, self.verifier = clock, verifier
        self.school_reader = school_reader

    @contextmanager
    def locked(self, name="lock", nonblocking=False):
        self.run.mkdir(parents=True, exist_ok=True, mode=0o755)
        path = self.run / name
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                raise Refusal("password_checking") from None
            yield
        finally:
            os.close(fd)

    def config(self):
        config = read(self.etc / "config.json", {"version": 2, "authentication": {"pin_enabled": False, "pin": None},
            "active_profile": "default", "profiles": {"default": DEFAULT_PROFILE}, "users": {}, "providers": {}})
        for profile in config["profiles"].values():
            profile.setdefault("respect_school_mode", False)
        return config

    def account(self, uid, config):
        try:
            user = pwd.getpwuid(uid)
        except KeyError:
            raise Refusal("unknown_user") from None
        if uid == 0 or user.pw_name not in config["users"]:
            raise Refusal("not_managed")
        key = config["users"][user.pw_name]["profile"]
        return user, key, config["profiles"][key]

    def day(self, user, key, profile):
        now = datetime.fromtimestamp(self.clock())
        today = now.date().isoformat()
        budget = profile["budget_minutes"][("mon", "tue", "wed", "thu", "fri", "sat", "sun")[now.weekday()]] * 60
        path = self.state / "users" / str(user.pw_uid) / (today + ".json")
        day = read(path, {"day": today, "spent_seconds": 0, "credited_seconds": 0, "granted_seconds": 0,
            "credit_totals": {}, "credit_receipts": {}, "warned": [], "agreement_noted": False, "ledger": []})
        day.update(profile=key, budget_seconds=budget)
        return path, day

    def runtime(self, uid):
        return {**deepcopy(DEFAULT_RUNTIME), **read(self.run / str(uid) / "runtime.json", {})}

    def school(self, user, profile):
        return self.school_reader(user.pw_name, profile.get("respect_school_mode") is True, self.clock())

    @staticmethod
    def remaining(day):
        return max(0, day["budget_seconds"] + day["credited_seconds"] + day["granted_seconds"] - day["spent_seconds"])

    def publish(self, user, profile, day, config):
        directory = self.run / str(user.pw_uid)
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        if os.geteuid() == 0:
            os.chown(directory, 0, user.pw_gid)
        os.chmod(directory, 0o750)
        status = read(directory / "status.json", {})
        status.update(ok=True, plugin_id=PLUGIN_ID, api_version=API_VERSION, user=user.pw_name, day=day["day"],
            remaining_seconds=self.remaining(day), budget_seconds=day["budget_seconds"], spent_seconds=day["spent_seconds"],
            credited_seconds=day["credited_seconds"], granted_seconds=day["granted_seconds"],
            pin_enabled=config["authentication"]["pin_enabled"])
        # Mark stale countdowns unusable until the daemon's next tick.
        status["lock_in_seconds"] = None
        rt = self.runtime(user.pw_uid)
        session = rt["session"]
        moment = datetime.fromtimestamp(self.clock()).strftime("%H:%M")
        blocking = next((p for p in profile["blocked_periods"] if p["enabled"] and
            (p["start"] <= moment < p["end"] if p["start"] < p["end"] else moment >= p["start"] or moment < p["end"])), None)
        school = self.school(user, profile)
        in_use = session.get("present") and session.get("active") and not session.get("locked")
        if rt.get("parent_lock_requested"):
            phase = "parent-lock"
        elif rt["paused"]:
            phase = "paused"
        elif profile["philosophy"] == "limits" and blocking:
            phase = "bedtime"
        elif school["active"]:
            phase = "school"
        elif profile["philosophy"] == "limits" and self.remaining(day) <= 0:
            phase = "empty"
        else:
            phase = "running" if in_use else "idle"
        status.update(phase=phase, counting=phase == "running", philosophy=profile["philosophy"], school_mode=school,
            reflections=[{"t": item["t"], "text": item["meta"]["text"]} for item in day["ledger"] if item["kind"] == "reflection"][-20:])
        status["credits"] = {"enabled": profile["credits"]["enabled"] and not school["active"] and not rt.get("parent_lock_requested", False), "cap_seconds": profile["credits"]["daily_cap_minutes"] * 60,
            "room_seconds": max(0, profile["credits"]["daily_cap_minutes"] * 60 - day["credited_seconds"]),
            "providers": {key: {**policy, "credited_today_seconds": day["credit_totals"].get(key, 0)}
                for key, policy in profile["credits"]["providers"].items()}}
        write(directory / "status.json", status, 0o640, user.pw_gid)

    def authenticate(self, uid, message, config):
        credential = message.get("credential", "")
        method = message.get("auth_method", "password")
        if method not in ("password", "pin") or not isinstance(credential, str) or len(credential) > 1024 or "\x00" in credential:
            raise Refusal("invalid_credential")
        if method == "pin" and not config["authentication"]["pin_enabled"]:
            raise Refusal("pin_disabled")
        with self.locked("auth.lock", nonblocking=True):
            path = self.state / "authentication" / (str(uid) + ".json")
            state = read(path, {"failures": 0, "until": 0})
            if self.clock() < state["until"]:
                raise Refusal("password_locked_out", retry_in_seconds=math.ceil(state["until"] - self.clock()))
            try:
                accepted = bool(credential) and (verify_pin(credential, config["authentication"]["pin"])
                    if method == "pin" else self.verifier(credential))
            except OSError:
                raise Refusal("authentication_unavailable") from None
            if accepted:
                write(path, {"failures": 0, "until": 0})
                return
            failures = min(state["failures"] + 1, 100)
            delays = (0, 0, 1, 5, 15, 60, 300)
            delay = delays[failures] if failures < len(delays) else min(3600, 300 * 2 ** (failures - 6))
            write(path, {"failures": failures, "until": self.clock() + delay})
            raise Refusal("bad_pin" if method == "pin" else "bad_password")

    def settings(self, profile, config):
        return {"ok": True, "plugin_id": PLUGIN_ID, "api_version": API_VERSION, "profile": deepcopy(profile),
            "pin_enabled": config["authentication"]["pin_enabled"], "providers": deepcopy(config["providers"])}

    def request(self, uid, message):
        try:
            if not isinstance(message, dict):
                raise Refusal("invalid_request")
            config = self.config()
            self.account(uid, config)
            command = message.get("cmd")
            if command not in ("config.get", "config.patch", "pin.set", "grant", "pause", "resume", "lock", "reflect", "forget"):
                raise Refusal("unknown_command")
            if command not in ("reflect", "forget"):
                self.authenticate(uid, message, config)
            with self.locked():
                current = self.config()
                if current["authentication"] != config["authentication"]:
                    raise Refusal("credentials_changed")
                user, key, profile = self.account(uid, current)
                path, day = self.day(user, key, profile)
                rt = self.runtime(uid)
                if command == "config.get":
                    return self.settings(profile, current)
                if command == "config.patch":
                    profile = patch_profile(profile, message.get("patch"), current["providers"])
                    current["profiles"][key] = profile
                    write(self.etc / "config.json", current)
                    _path, day = self.day(user, key, profile)
                    self.publish(user, profile, day, current)
                    return self.settings(profile, current)
                if command == "pin.set":
                    enabled, pin = message.get("enabled"), message.get("new_pin", "")
                    if type(enabled) is not bool or not isinstance(pin, str):
                        raise Refusal("invalid_pin")
                    if enabled and not re.fullmatch(r"[0-9]{4,12}", pin):
                        raise Refusal("pin_must_be_4_to_12_digits")
                    current["authentication"] = {"pin_enabled": enabled, "pin": pin_record(pin) if enabled else None}
                    write(self.etc / "config.json", current)
                    self.publish(user, profile, day, current)
                    return self.settings(profile, current)
                if command == "grant":
                    minutes = message.get("minutes")
                    if not integer(minutes, -600, 600):
                        raise Refusal("bad_minutes")
                    day["granted_seconds"] += minutes * 60
                    day["ledger"].append({"t": self.clock(), "kind": "grant", "seconds": minutes * 60, "meta": {"by": "parent"}})
                elif command in ("pause", "resume"):
                    rt["paused"] = command == "pause"
                    rt["lock_after"] = None
                    day["ledger"].append({"t": self.clock(), "kind": command, "meta": {"by": "parent"}})
                elif command == "lock":
                    # The daemon uses its existing verified lock/retry path.
                    rt["parent_lock_requested"] = True
                elif command == "reflect":
                    text = message.get("text")
                    if profile["philosophy"] != "together" or not isinstance(text, str) or not text.strip() or len(text) > 280:
                        raise Refusal("invalid_note")
                    if sum(item["kind"] == "reflection" for item in day["ledger"]) >= 20:
                        raise Refusal("too_many_notes")
                    day["ledger"].append({"t": self.clock(), "kind": "reflection", "meta": {"text": text}})
                elif command == "forget":
                    stamp = message.get("t")
                    if type(stamp) not in (int, float) or not math.isfinite(stamp):
                        raise Refusal("invalid_note")
                    day["ledger"] = [item for item in day["ledger"] if not (item["kind"] == "reflection" and item["t"] == stamp)]
                if self.remaining(day) > 0 and command != "lock":
                    rt.update(blocked_since=None, lock_after=None, lock_count=0, last_lock_ok=False, lock_failures=0)
                write(path, day)
                write(self.run / str(uid) / "runtime.json", rt)
                self.publish(user, profile, day, current)
                return {"ok": True, "remaining_seconds": self.remaining(day)}
        except Refusal as error:
            return error.result

    def award(self, username, provider, receipt, day_key):
        """Root-only entry point, intentionally absent from request(). A trusted
        game backend has already verified the completion before calling it.
        Credits and replay receipts commit together, independently of logs.
        """
        try:
            if not isinstance(provider, str) or len(provider) > 96 or not PROVIDER_ID.fullmatch(provider):
                raise Refusal("invalid_provider")
            if not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt):
                raise Refusal("invalid_receipt")
            with self.locked():
                config = self.config()
                user, key, profile = self.account(pwd.getpwnam(username).pw_uid, config)
                path, day = self.day(user, key, profile)
                if day_key != day["day"]:
                    raise Refusal("wrong_day", day=day["day"])
                if provider not in config["providers"]:
                    raise Refusal("unregistered_provider")
                receipt_key = provider + ":" + receipt
                if receipt_key in day["credit_receipts"]:
                    return {**day["credit_receipts"][receipt_key], "already_credited": True}
                if len(day["credit_receipts"]) >= 4096:
                    raise Refusal("receipt_capacity_reached")
                rt = self.runtime(user.pw_uid)
                credits = profile["credits"]
                policy = credits["providers"].get(provider, {})
                moment = datetime.fromtimestamp(self.clock()).strftime("%H:%M")
                blocked = any(period["enabled"] and (period["start"] <= moment < period["end"] if period["start"] < period["end"]
                    else moment >= period["start"] or moment < period["end"]) for period in profile["blocked_periods"])
                session = rt.get("session", {})
                reason = ""
                if not credits["enabled"] or not policy.get("enabled"):
                    reason = "credits_disabled"
                elif self.school(user, profile)["active"]:
                    reason = "school_mode_active"
                elif profile["philosophy"] != "limits" or rt.get("paused") or rt.get("parent_lock_requested") or blocked:
                    reason = "policy_blocked"
                elif not session.get("present") or not session.get("active") or session.get("locked") or not 0 <= self.clock() - rt.get("updated_at", 0) <= 30:
                    reason = "session_unavailable"
                seconds = 0 if reason else max(0, min(policy["seconds_per_event"],
                    credits["daily_cap_minutes"] * 60 - day["credited_seconds"],
                    policy["daily_cap_minutes"] * 60 - day["credit_totals"].get(provider, 0)))
                result = {"ok": True, "api_version": API_VERSION, "provider": provider, "receipt": receipt,
                    "day": day_key, "credited_seconds": seconds, "already_credited": False,
                    "reason": reason or ("daily_cap_reached" if seconds == 0 else "credited")}
                day["credit_receipts"][receipt_key] = result
                day["credited_seconds"] += seconds
                day["credit_totals"][provider] = day["credit_totals"].get(provider, 0) + seconds
                if seconds:
                    day["ledger"].append({"t": self.clock(), "kind": "credit", "seconds": seconds,
                        "meta": {"provider": provider, "name": config["providers"][provider]["name"]}})
                write(path, day)
                if seconds and self.remaining(day) > 0:
                    rt.update(blocked_since=None, lock_after=None, lock_count=0, last_lock_ok=False, lock_failures=0)
                    write(self.run / str(user.pw_uid) / "runtime.json", rt)
                self.publish(user, profile, day, config)
                return result
        except KeyError:
            return {"ok": False, "error": "unknown_user"}
        except Refusal as error:
            return error.result


def patch_profile(profile, patch, providers):
    if not isinstance(patch, dict) or not patch or set(patch) - set(DEFAULT_PROFILE):
        raise Refusal("invalid_patch")
    result = deepcopy(profile)
    for key, value in patch.items():
        valid = False
        if key in ("name", "agreement_text"):
            valid = isinstance(value, str) and len(value) <= (40 if key == "name" else 500)
        elif key == "philosophy":
            valid = value in ("limits", "together")
        elif key == "respect_school_mode":
            valid = type(value) is bool
        elif key == "on_empty":
            valid = value in ("lock", "notify")
        elif key in ("agreement_minutes", "break_nudge_minutes", "grace_seconds", "relock_seconds", "unlock_grace_seconds"):
            valid = integer(value, 5 if key in ("relock_seconds", "unlock_grace_seconds") else 0, 3600 if "seconds" in key else 480 if key == "break_nudge_minutes" else 1440)
        elif key == "warn_minutes":
            valid = isinstance(value, list) and 1 <= len(value) <= 8 and all(integer(v, 1, 1440) for v in value)
        elif key == "budget_minutes":
            valid = isinstance(value, dict) and not set(value) - set(DEFAULT_PROFILE[key]) and all(integer(v, 0, 1440) for v in value.values())
            if valid:
                value = {**result[key], **value}
        elif key == "blocked_periods":
            valid = isinstance(value, list) and len(value) <= 8
            if valid:
                for period in value:
                    valid = isinstance(period, dict) and set(period) == {"label", "enabled", "start", "end"}
                    valid = valid and isinstance(period["label"], str) and 0 < len(period["label"]) <= 40 and type(period["enabled"]) is bool
                    valid = valid and all(isinstance(period[k], str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", period[k]) for k in ("start", "end"))
                    if not valid or period["start"] == period["end"]:
                        valid = False
                        break
        elif key == "credits":
            valid = isinstance(value, dict) and not set(value) - {"enabled", "daily_cap_minutes", "providers"}
            if valid:
                value = {**result[key], **value}
                valid = type(value["enabled"]) is bool and integer(value["daily_cap_minutes"], 0, 1440) and isinstance(value["providers"], dict)
            if valid:
                valid = not set(value["providers"]) - set(providers)
                for policy in value["providers"].values():
                    valid = valid and isinstance(policy, dict) and set(policy) == {"enabled", "seconds_per_event", "daily_cap_minutes"}
                    valid = valid and type(policy["enabled"]) is bool and integer(policy["seconds_per_event"], 1, 3600) and integer(policy["daily_cap_minutes"], 0, 1440)
        if not valid:
            raise Refusal("invalid_patch", field=key)
        result[key] = deepcopy(value)
    return result
