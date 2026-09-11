"""Read the standalone School Mode service's root-owned status, never its UI.

Both accounting and credit decisions use this reader. A missing, stopped or
untrusted School Mode service must not grant a free-time budget exemption.
Tests can supply a temporary root and owner; production has no environment
override and always uses the installed controls service's status directory.
"""
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

CONTROLS_ROOT = Path("/var/lib/omarchy-kids-controls")
MAX_AGE = 30
MAX_BYTES = 32768


def snapshot(username, enabled, now=None, *, root=CONTROLS_ROOT, owner=0):
    result = {"linked": enabled is True, "available": False, "active": False, "reason": "disabled"}
    if enabled is not True:
        return result
    result["reason"] = "unavailable"
    if not isinstance(username, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        return {**result, "reason": "invalid"}
    now = time.time() if now is None else now
    try:
        with ExitStack() as descriptors:
            parent = None
            # Opening each component relative to its checked parent prevents
            # following user-supplied directory or file symlinks.
            for part in (root, "status", username, "school-mode"):
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.callback(os.close, fd)
                info = os.fstat(fd)
                if info.st_uid != owner or info.st_mode & 0o022:
                    return {**result, "reason": "untrusted"}
                parent = fd
            fd = os.open("status.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            descriptors.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
                return {**result, "reason": "untrusted"}
            if info.st_size > MAX_BYTES:
                return {**result, "reason": "invalid"}
            data = json.loads(os.read(fd, MAX_BYTES + 1))
        if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
            return {**result, "reason": "invalid"}
        stamp = data.get("updatedAt")
        if (type(data.get("enabled")) is not bool or data.get("mode") not in ("school", "free")
                or type(stamp) not in (int, float) or not math.isfinite(stamp)):
            return {**result, "reason": "invalid"}
        if not (-5 <= now - stamp <= MAX_AGE and -5 <= now - info.st_mtime <= MAX_AGE):
            return {**result, "reason": "stale"}
        if not data["enabled"]:
            return {**result, "reason": "not_enrolled"}
        return {**result, "available": True, "active": data["mode"] == "school", "reason": data["mode"]}
    except FileNotFoundError:
        return result
    except (ValueError, UnicodeError, RecursionError):
        return {**result, "reason": "invalid"}
    except OSError:
        return {**result, "reason": "untrusted"}


if __name__ == "__main__":
    # Called only by the root daemon, using its enrolled account and clock.
    print(json.dumps(snapshot(sys.argv[1], True, float(sys.argv[2]))))
