"""Fixed entry points for installed root commands. Never load user plugins."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import Core, DEFAULT_PROFILE, PROVIDER_ID, Refusal, deepcopy, write
from install import assert_no_conflict


def main(argv=None):
    if os.geteuid() != 0:
        return {"ok": False, "error": "root_required"}
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("control")
    award = sub.add_parser("award")
    for option in ("user", "provider", "receipt", "day"):
        award.add_argument("--" + option, required=True)
    admin = sub.add_parser("admin")
    admin.add_argument("action", choices=("enroll", "remove", "provider-register", "provider-remove", "pin-reset", "status"))
    admin.add_argument("value", nargs="?")
    admin.add_argument("--name")
    admin.add_argument("--unit", default="completed activity")
    args = parser.parse_args(argv)
    core = Core()
    if args.mode == "control":
        caller = os.environ.get("SUDO_UID", "")
        if not caller.isdecimal() or not 0 < int(caller) < 2 ** 31:
            return {"ok": False, "error": "invalid_caller"}
        line = sys.stdin.buffer.readline(65537)
        if len(line) > 65536:
            return {"ok": False, "error": "request_too_large"}
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            return {"ok": False, "error": "invalid_json"}
        return core.request(int(caller), request)
    if args.mode == "award":
        return core.award(args.user, args.provider, args.receipt, args.day)
    with core.locked():
        config = core.config()
        if args.action in ("enroll", "remove"):
            user = pwd.getpwnam(args.value or "")
            if user.pw_uid == 0:
                raise Refusal("choose_child_user")
            if args.action == "enroll":
                assert_no_conflict(user.pw_name)
                profile = deepcopy(DEFAULT_PROFILE)
                profile["name"] = user.pw_name
                config["profiles"].setdefault(user.pw_name, profile)
                config["users"][user.pw_name] = {"profile": user.pw_name}
                subprocess.run(["/usr/bin/usermod", "-aG", "peterholko-screen-time", user.pw_name], check=True)
            else:
                config["users"].pop(user.pw_name, None)
                subprocess.run(["/usr/bin/gpasswd", "-d", user.pw_name, "peterholko-screen-time"], check=False, capture_output=True)
                directory = core.run / str(user.pw_uid)
                if directory.is_dir() and not directory.is_symlink():
                    shutil.rmtree(directory)
        elif args.action in ("provider-register", "provider-remove"):
            if not args.value or len(args.value) > 96 or not PROVIDER_ID.fullmatch(args.value):
                raise Refusal("invalid_provider")
            if args.action == "provider-register":
                if not args.name or len(args.name) > 60 or len(args.unit) > 40:
                    raise Refusal("invalid_provider_description")
                if args.value not in config["providers"] and len(config["providers"]) >= 32:
                    raise Refusal("too_many_providers")
                config["providers"][args.value] = {"name": args.name, "unit": args.unit}
            else:
                config["providers"].pop(args.value, None)
                for profile in config["profiles"].values():
                    profile["credits"]["providers"].pop(args.value, None)
        elif args.action == "pin-reset":
            config["authentication"] = {"pin_enabled": False, "pin": None}
        elif args.action == "status":
            config["authentication"].pop("pin", None)
            return {"ok": True, "config": config}
        write(core.etc / "config.json", config)
        return {"ok": True}


if __name__ == "__main__":
    try:
        result = main()
    except Refusal as error:
        result = error.result
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        result = {"ok": False, "error": "service_error"}
    print(json.dumps(result))
    raise SystemExit(0 if result.get("ok") else 1)
