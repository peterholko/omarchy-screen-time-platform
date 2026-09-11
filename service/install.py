"""Explicit, local-only service installation. No package downloads or CI jobs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

NAME = "peterholko-screen-time"
PAYLOAD = Path("/usr/lib") / NAME
ETC = Path("/etc") / NAME
STATE = Path("/var/lib") / NAME
UNIT = NAME + ".service"
RECORD = STATE / "installation.json"
CONFLICTS = (
    "/etc/omarchy-kids-controls/screen-time.json",
    "/etc/omarchy/parent/screen-time.json",
    "/etc/omarchy-screen-time/config.json",
)


def assert_no_conflict(username, root=Path("/")):
    for relative in CONFLICTS:
        path = root / relative.lstrip("/")
        if path.exists():
            data = json.loads(path.read_text())
            if username in data.get("users", {}):
                raise ValueError(f"{username} is already enrolled in {relative}. Remove that account from the other screen-time roster before enrolling it here. Nothing was changed.")


def safe_target(path):
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise ValueError(f"Refusing a symlink at {entry}")
        if entry.exists():
            metadata = entry.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError(f"Expected a root-owned, non-writable system path: {entry}")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def wrapper(script, mode="", control=False):
    # Only sudo's caller identity crosses the privileged control boundary.
    identity = ' SUDO_UID="${SUDO_UID:-}"' if control else ""
    arguments = "" if control else ' "$@"'
    guard = 'if (( $# != 0 )); then exit 2; fi\n' if control else ""
    return ("#!/bin/bash\nset -euo pipefail\n" + guard +
        f'exec /usr/bin/env -i PATH=/usr/bin:/bin{identity} /usr/bin/python3 -I {PAYLOAD}/{script}' +
        (" " + mode if mode else "") + arguments + "\n").encode()


def payload(source, omarchy_path):
    files = {}
    for directory in ("service", "parent"):
        for item in sorted((source / directory).iterdir()):
            if item.name.startswith(".") or item.name == "__pycache__":
                continue
            if not item.is_file() or item.is_symlink():
                raise ValueError(f"Unexpected source file: {item}")
            files[PAYLOAD / directory / item.name] = (item.read_bytes(), 0o644)
    for name, data in {
        "omarchy-peterholko-screen-time-ctl": wrapper("service/runtime.py", "control", True),
        "omarchy-peterholko-screen-time-credit": wrapper("service/runtime.py", "award"),
        "omarchy-peterholko-screen-time-admin": wrapper("service/runtime.py", "admin"),
        # The unprivileged GUI needs the desktop environment. Python -I still
        # prevents importing child-controlled Python modules.
        "omarchy-peterholko-screen-time": b'#!/bin/bash\nexec /usr/bin/python3 -I /usr/lib/peterholko-screen-time/service/client.py "$@"\n',
    }.items():
        files[Path("/usr/bin") / name] = (data, 0o755)
    files[Path("/etc/systemd/system") / UNIT] = ((source / "service" / UNIT).read_bytes(), 0o644)
    files[Path("/etc/pam.d") / NAME] = ((source / "service/pam.conf").read_bytes(), 0o644)
    # The empty argument string is sudoers syntax for NO arguments, rather
    # than a wildcard permitting arbitrary arguments. Credits are absent.
    sudoers = f'%{NAME} ALL=(root) NOPASSWD: /usr/bin/omarchy-peterholko-screen-time-ctl ""\n'
    files[Path("/etc/sudoers.d") / NAME] = (sudoers.encode(), 0o440)
    escaped = omarchy_path.replace("\\", "\\\\").replace('"', '\\"')
    files[ETC / "desktop.env"] = (f'OMARCHY_PATH="{escaped}"\n'.encode(), 0o600)
    return files


def check_existing(files, previous, upgrade):
    for path, (data, _mode) in files.items():
        safe_target(path)
        if path.exists():
            actual = digest(path.read_bytes())
            if actual == digest(data):
                continue
            if not upgrade or previous.get(str(path)) != actual:
                raise ValueError(f"Refusing to replace an untracked or locally modified file: {path}. Use --upgrade for a previously installed version.")
    for old in set(previous) - {str(path) for path in files}:
        path = Path(old)
        if not path.is_relative_to(PAYLOAD):
            raise ValueError(f"Unexpected obsolete installation file: {path}")
        safe_target(path)
        if path.exists() and digest(path.read_bytes()) != previous[old]:
            raise ValueError(f"Locally modified obsolete file: {path}")


def atomic_file(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--omarchy-path", required=True)
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or not sys.platform.startswith("linux"):
        raise ValueError("Run ./setup on Omarchy Linux, from a parent-authorized terminal.")
    user = pwd.getpwnam(args.user)
    if user.pw_uid == 0:
        raise ValueError("Choose a child account, not root.")
    if not Path(args.omarchy_path).is_absolute() or any(ord(c) < 32 for c in args.omarchy_path):
        raise ValueError("OMARCHY_PATH must be an absolute path without control characters.")
    if not (Path(args.omarchy_path) / "bin/omarchy-notification-send").is_file():
        raise ValueError("OMARCHY_PATH does not contain an Omarchy installation.")
    assert_no_conflict(user.pw_name)
    for command in ("jq", "inotifywait", "flock", "loginctl", "runuser", "visudo", "systemctl", "getent", "usermod"):
        if not shutil.which(command):
            raise ValueError(f"Missing {command}. Install dependencies with: omarchy pkg add python python-pyside6 jq inotify-tools")
    subprocess.run(["/usr/bin/python3", "-I", "-c", "from PySide6 import QtWidgets"], check=True)
    shadow = subprocess.run(["/usr/bin/getent", "shadow", "root"], capture_output=True, text=True, check=True).stdout.split(":")
    if len(shadow) < 2 or not shadow[1] or shadow[1].startswith(("!", "*")):
        raise ValueError("The parent/root password is not set or is locked. Set it from an administrator terminal with sudo passwd root, then rerun setup. Setup will not change a password.")
    for directory in (ETC, STATE, Path("/run") / NAME):
        safe_target(directory)
    safe_target(RECORD)
    previous = json.loads(RECORD.read_text()).get("files", {}) if RECORD.exists() else {}
    source = Path(__file__).resolve().parents[1]
    files = payload(source, args.omarchy_path)
    check_existing(files, previous, args.upgrade)
    with tempfile.NamedTemporaryFile() as check:
        check.write(files[Path("/etc/sudoers.d") / NAME][0]); check.flush()
        subprocess.run(["/usr/bin/visudo", "-cf", check.name], check=True)
    # Preflight is complete before stopping an existing version or writing.
    subprocess.run(["/usr/bin/systemctl", "stop", UNIT], check=False, capture_output=True)
    for directory, mode in ((ETC, 0o700), (STATE, 0o700), (Path("/run") / NAME, 0o755)):
        directory.mkdir(parents=True, exist_ok=True, mode=mode); directory.chmod(mode)
    if subprocess.run(["/usr/bin/getent", "group", NAME], capture_output=True).returncode:
        subprocess.run(["/usr/bin/groupadd", "--system", NAME], check=True)
    for path, (data, mode) in files.items():
        atomic_file(path, data, mode)
    for old in set(previous) - {str(path) for path in files}:
        Path(old).unlink(missing_ok=True)
    atomic_file(RECORD, json.dumps({"version": "1.0.0", "files": {str(path): digest(data) for path, (data, _) in files.items()}}).encode(), 0o600)
    subprocess.run(["/usr/bin/omarchy-peterholko-screen-time-admin", "enroll", user.pw_name], check=True)
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
    subprocess.run(["/usr/bin/systemctl", "enable", "--now", UNIT], check=True)
    print(f"Screen Time installed for {user.pw_name}. Log out and back in for group membership, then open the Screen Time widget or omarchy-peterholko-screen-time parent.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
