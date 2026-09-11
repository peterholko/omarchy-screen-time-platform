# Screen Time credit API v1

Plugin ID: `peterholko.screen-time`. Read-only discovery: `omarchy-peterholko-screen-time status`, which returns `plugin_id` and `api_version: 1` for an enrolled account. The root-owned daily ledger is the source of truth; the QML countdown is a display of that state.

## Trust boundary

A game owns its activities, challenge generation and completion verification. Its optional, root-owned verifier validates a completion and then invokes the platform. The platform owns budgets, permissions, rates, caps, receipts and the credit ledger. It contains no questions, answers, grade tables or scoring engine.

The credit helper is **root-only** and has no passwordless sudo rule. A child-side game, console or shell plugin cannot call it directly to award time. Registration is also a root operation. Only install verifiers you trust: root services share the administrator's authority; provider IDs are names, not credentials or sandboxes.

Do not install a privileged wrapper that simply forwards arbitrary child-supplied receipts to the credit command. Do not accept a frontend's `correct: true`, self-reported score, duration or selected username as proof. The verifier must derive the child identity from Unix peer credentials, issue and validate its own challenges, and keep its state outside the child's writable directories.

## Register a provider

The game's explicit, parent-authorized setup registers its stable provider ID and a human-readable activity unit:

```bash
sudo omarchy-peterholko-screen-time-admin provider-register peterholko.pawberry \
  --name 'Pawberry Pet Hotel' --unit 'completed problem'
```

Registration does **not** enable credits. A parent opens Screen Time → Connected games, enables the overall switch and the provider, and selects seconds per completion and a daily maximum. Existing per-account settings survive re-registration. Up to 32 providers may be registered. IDs use lowercase dot-separated components, digits and hyphens; maximum 96 characters.

Remove registration with `sudo omarchy-peterholko-screen-time-admin provider-remove peterholko.pawberry`. This removes the provider's current settings but retains its daily credit totals and receipts, preventing re-registration from resetting today's caps.

## Submit a verified completion

After validating a complete activity, the game verifier durably stores a pending record with the child account, provider ID, unique receipt and local calendar day. It then calls:

```bash
# Invoked from the game's trusted root service, not from its frontend.
/usr/bin/omarchy-peterholko-screen-time-credit \
  --user linnea \
  --provider peterholko.pawberry \
  --receipt 54ba1dce0e1c4b9f9a759e87eaf6957b \
  --day 2026-09-10
```

The day above is an example; use the completion day's actual local date. Receipts must contain 16–128 ASCII letters, digits, underscores or hyphens. A random UUID stored once per verified completion is suitable. The helper accepts **no reward amount**. Use the installed [sdk.py](../service/sdk.py) from the trusted service, or vendor this small adapter into the game's verifier:

```python
from sdk import credit_completion

# pending was already persisted by the game's verifier.
result = credit_completion(
    user=pending["user"],
    provider="peterholko.pawberry",
    receipt=pending["receipt"],
    day=pending["day"],
)
```

An accepted receipt returns:

```json
{"ok":true,"api_version":1,"provider":"peterholko.pawberry","receipt":"54ba1dce0e1c4b9f9a759e87eaf6957b","day":"2026-09-10","credited_seconds":60,"already_credited":false,"reason":"credited"}
```

Credits equal the smallest of the configured per-event reward, the provider's remaining daily allowance and the remaining allowance across all games. The last award may be partial. Manual parent grants do not consume game allowances. Changing budgets or switching modes does not erase earned credits, spent time or receipts.

Accepted zero-credit results use `reason` values `credits_disabled`, `school_mode_active`, `policy_blocked`, `session_unavailable` or `daily_cap_reached`. They are final for that receipt. A fresh daemon session observation (within 30 seconds) is required; a stopped service's stale state cannot enable credits. An activity completed while credits are disabled or blocked does not become eligible when a parent later changes the settings. `school_mode_active` means the parent enabled the optional School Mode connection and the separate service currently reports School Mode. Resubmitting that receipt in Free Time still returns zero.

## Retries, durability and limits

The helper serializes with the daemon and parent updates using the same file lock. The credited total, per-provider total and receipt result are written together with atomic replacement and fsync. Repeating the same `(account, day, provider, receipt)` returns the original result with `already_credited: true`, including the original `credited_seconds`. That field describes the original credit; it is not an additional award. Concurrent retries add time once.

After a timeout, a process failure or an unreadable reply, retry the **same persisted receipt and day**. Never create a new receipt just because a response was lost. Mark the pending game record complete only after receiving and saving a terminal result. Retrying after publication failed but the ledger committed still returns the committed result.

`ok: false` responses include `not_managed`, `unregistered_provider`, `invalid_provider`, `invalid_receipt`, `wrong_day`, `receipt_capacity_reached`, `root_required` and `service_error`. A receipt for another day is refused without changing today's balance. Expire it without regenerating it under today's date. Retain an unresolved acknowledgement for diagnostics rather than claiming that the player earned minutes. A daily maximum of 4,096 receipts prevents unbounded growth; reaching it refuses new receipts instead of dropping old duplicate protection.

If the platform is absent or disabled, the game continues ordinary play, collections and learning progress. It must not show a success message claiming screen-time minutes. Display the actual credited amount only after a confirmed positive result.

## Read-only status and parent settings

Public status includes remaining, budget, spent, credited and parent-granted seconds, mode, phase, and `credits` containing the overall switch, cap, remaining allowance and per-provider policy/totals. It contains no parent password, PIN hash or authentication attempt details. Files are root-owned and readable for the enrolled account's group.

Since plugin version 1.1.0, public status also includes `school_mode: {linked, available, active, reason}`. The `school` phase indicates paused school-time accounting; bedtime, explicit parent locks (`parent-lock`) and a parent pause take precedence in the displayed phase. `credits.enabled` is an effective availability flag: it becomes false during connected School Mode or a pending parent lock without changing the saved parent preference. Existing v1 game adapters that honor this flag require no new endpoint. The root credit helper independently checks current School Mode status for every new receipt.

The optional `respect_school_mode` profile setting is a strict boolean, defaults to false for new and existing profiles, and can only be changed through authenticated `config.patch`. The reader accepts the original School Mode service's `schemaVersion: 1` status at `/var/lib/omarchy-kids-controls/status/ACCOUNT/school-mode/status.json`. It requires root ownership, no symlinks or group/other write access, an enrolled account, and both a status timestamp and file modification time within 30 seconds (with five seconds of clock tolerance). An unavailable source never exempts time from the budget. The connection has no write access to School Mode policy through this API.

The parent application alone uses the credential-protected control transport for `config.get`, `config.patch`, `pin.set`, `grant`, `pause`, `resume` and `lock`. Agreement notes use `reflect` and `forget` without a parent credential. There is no game credit or quiz operation on that transport. Sudo supplies the caller identity; a payload cannot select another child's account.

## Installed paths

| Purpose | Location |
| --- | --- |
| Root service and parent application | `/usr/lib/peterholko-screen-time/` |
| Config, optional PIN hash and desktop environment | `/etc/peterholko-screen-time/` |
| Usage ledger and authentication throttling | `/var/lib/peterholko-screen-time/` |
| Lock and public status | `/run/peterholko-screen-time/` |
| System service | `peterholko-screen-time.service` |
| Parent/control client | `/usr/bin/omarchy-peterholko-screen-time` |
| No-argument credential-checking wrapper | `/usr/bin/omarchy-peterholko-screen-time-ctl` |
| Root completion helper | `/usr/bin/omarchy-peterholko-screen-time-credit` |
| Root enrollment and registration helper | `/usr/bin/omarchy-peterholko-screen-time-admin` |

The installer has no hooks that download or execute remote code. Adding a provider never executes that provider's code inside the platform daemon.
