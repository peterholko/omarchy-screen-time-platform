# Verification

Local verification uses temporary files and synthetic account/session observations. It does not install a root service on the development Mac or launch an ISO. The repository contains no GitHub Actions workflows.

- Python tests cover parent-password defaults, optional PIN authentication and throttling, Agreement settings protection, per-account identity, private/public permissions, credit caps, concurrent retries, durable receipts, rollover and failure recovery.
- Daemon tests execute the extracted Bash daemon's real jq transition program, including budget exhaustion, bedtime, warnings, pause, Agreement mode, explicit parent locks, retries, termination fallback and preservation of credit receipts. They also exercise custom profile selection and paths with spaces.
- Installer tests cover per-account conflicts, local edits and naming collisions, symlink refusal, the no-argument sudo transport, and the separation of privileged credit commands from child-accessible controls.
- `test/parent_visual.py` runs the real Qt parent window against the core with a fixture password verifier. It exercises checking feedback, a rejected password, saving limits, pause/resume, game policies, enabling and disabling a PIN, and PIN authentication. Its screenshots are inspected at normal and compact window sizes.
- The three QML files are parsed with Qt's QML formatter, and the community manifest is checked against Omarchy's marketplace validator. The widget's service lookup follows the community plugin shell facade.

Still requires a real Omarchy laptop check: the distribution's PAM stack and root password, first setup and group membership, systemd hardening, a real Quickshell bar/countdown, actual session lock confirmation and Hyprland/logout behavior. Those cannot be established by the Mac's fixture tests. Test with a short budget and a parent available before relying on enforcement.

No changes to the three games are implied by passing the platform tests; their future integrations have their own acceptance requirements in the [integration plan](game-integration-plan.md).
