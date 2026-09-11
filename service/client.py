"""Unprivileged status reader and fixed, password-on-stdin control transport."""
import json
import os
from pathlib import Path
import subprocess
import sys

RUN = Path("/run/peterholko-screen-time")
ROOT = Path("/usr/lib/peterholko-screen-time")


def read_status():
    try:
        return json.loads((RUN / str(os.getuid()) / "status.json").read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": "not_managed"}


def main(args):
    command = args[0] if args else "status"
    if command == "parent":
        os.execv("/usr/bin/python3", ["python3", "-I", str(ROOT / "parent/app.py"), "--page", args[1] if len(args) > 1 and args[1] in ("controls", "settings") else "controls"])
    if command == "status":
        return read_status()
    if command == "history":
        try:
            return {"ok": True, "days": json.loads((RUN / str(os.getuid()) / "history.json").read_text())}
        except (OSError, ValueError):
            return read_status()
    if command == "watch":
        print(json.dumps(read_status()), flush=True)
        directory = RUN / str(os.getuid())
        if not directory.is_dir():
            return None
        with subprocess.Popen(["/usr/bin/inotifywait", "-m", "-q", "-e", "close_write", "-e", "moved_to", "--format", "%f", str(directory)], stdout=subprocess.PIPE, text=True) as process:
            for name in process.stdout:
                if name.strip() == "status.json":
                    print(json.dumps(read_status()), flush=True)
        return None
    if command in ("control", "reflect", "forget"):
        if command == "control":
            line = sys.stdin.buffer.readline(65537)
            if len(line) > 65536:
                return {"ok": False, "error": "request_too_large"}
            try:
                payload = json.loads(line)
            except (ValueError, UnicodeError):
                return {"ok": False, "error": "invalid_json"}
        else:
            payload = {"cmd": command}
            if command == "reflect":
                payload["text"] = " ".join(args[1:])
            else:
                try:
                    payload["t"] = float(args[1])
                except (IndexError, ValueError):
                    return {"ok": False, "error": "invalid_note"}
        result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/omarchy-peterholko-screen-time-ctl"],
            input=json.dumps(payload), text=True, capture_output=True, timeout=30)
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {"ok": False, "error": "not_permitted"}
    return {"ok": False, "error": "unknown_command"}


if __name__ == "__main__":
    try:
        result = main(sys.argv[1:])
    except (OSError, subprocess.SubprocessError):
        result = {"ok": False, "error": "unavailable"}
    if result is not None:
        print(json.dumps(result))
        raise SystemExit(0 if result.get("ok") else 1)
