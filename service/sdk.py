"""API v1 for a game's root-owned completion verifier, never its UI.

Persist (provider, receipt, day) before calling; retry those exact values after
an uncertain result. The platform selects the configured reward, not the game.
"""
import json
import subprocess

PLUGIN_ID = "peterholko.screen-time"
API_VERSION = 1


def credit_completion(*, user, provider, receipt, day):
    try:
        result = subprocess.run(["/usr/bin/omarchy-peterholko-screen-time-credit",
            "--user", user, "--provider", provider, "--receipt", receipt, "--day", day],
            text=True, capture_output=True, timeout=15)
        response = json.loads(result.stdout)
        if isinstance(response, dict) and "ok" in response:
            return response
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"ok": False, "error": "unavailable", "retry_same_receipt": True}
