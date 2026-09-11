# Three-game integration design

This design has been implemented in the individual repositories: Pawberry Pet Hotel 1.6.0, Number Grove 1.1.0 and Paw Post Typing 1.1.0. The sections below retain the original implementation plan and describe the previous adapters as they existed when planning began. All three games remain independently installable and playable without Screen Time. The combined plugin and Omarchy Kids repository were not updated.

Screen Time 1.1.0 also supports an optional connection to the original School Mode plugin. Its effective credit availability flag and root credit policy pause rewards during connected School Mode, so these game releases need no additional API migration for that connection.

## Shared approach

Use `peterholko.screen-time` API v1 as the optional destination for verified completions. Keep game rules and challenge validation in each game's own backend. The new platform receives only a child account, registered provider, receipt and completion day. It decides the reward from the parent's configuration.

Each game's setup installs its verifier into a root-owned location and registers the provider. It must not enroll a second clock or start the old combined time daemon implicitly. Its child-side connection derives identity from peer credentials. A persistent pending receipt bridges the game's completed activity and the platform's atomic credit ledger; retries retain the exact receipt.

The game keeps its own opt-in for participation. Screen Time provides the authoritative reward rate and provider/global caps in its password-protected **Connected games** settings. An in-game link can open `omarchy-peterholko-screen-time parent settings`; the game must not read or store the parent credential in its Quickshell scene. With either opt-in off, gameplay still works and no minutes are claimed.

Provider IDs below are integration identifiers. They do not rename the games' current launcher plugin IDs.

## 1. Pawberry Pet Hotel — first integration

Provider: `peterholko.pawberry`. Unit: **completed problem**.

Pawberry already has a privileged practice service and a completion receipt. Its current `lib/parent/omarchy_kids/pawberry/rewards.py` calls the co-hosted time account's `credit_activity`. Its parent settings also depend on that host's screen-time service.

1. Package the Pawberry verifier so it can run without loading or enrolling the combined time module. Preserve its existing daily operation quotas, including addition, subtraction, multiplication and Mixed mode, and its validation of intermediate arithmetic steps.
2. Replace the direct `credit_activity` call with the v1 SDK. Extend the persisted completion receipt to include the selected backend, account, provider, receipt ID and local day. Save that record before submitting it; settle retries with the same ID after a restart.
3. Keep pets, accessories and ordinary progress tied to completing the problem. Award screen time only after the backend confirms every required intermediate step and final answer; collection rewards should still work when no time is available.
4. Keep the existing in-game parent controls for practice quotas. Change its screen-time section to show this platform's availability, actual credits and participation switch, with a link to the platform's parent settings for rates and caps. Remove assumptions that the old global math-earning switch must be enabled.
5. Test incomplete steps, wrong answers, operation quotas, repeated completion requests, Mixed-mode selection, cap changes during a problem, lost acknowledgements, midnight and both installations absent/present.

## 2. Number Grove — move its verifier out of Screen Time

Provider: `peterholko.number-grove`. Proposed unit: **completed challenge**.

The current reward bridge is a thin client for the old time service's `quiz.next` and `quiz.answer` operations. Those operations intentionally do not exist in the new platform. Pointing the existing bridge at the new service would not work.

1. Move the challenge-generation and answer-verification rules needed by Number Grove into its optional game backend. Keep grades, correct-seed selection, difficulty, timing and replay checks in Number Grove.
2. Replace `RewardBridge.qml` and the `number_grove/rewards.py` adapter's quiz calls with that backend's challenge/answer protocol. A child cannot choose a different account or supply a reward amount.
3. Define one verified challenge completion as one creditable event. The backend issues the challenge and validates the selected seed; it retains completion receipts and cannot award repeatedly for reopening a board or collecting the same seed again.
4. Submit the completion through the SDK and show the platform's confirmed amount. Calm/adventure play, the existing game art and practice without time rewards remain available.
5. Test seed collection keys, wrong selections, stale challenges, grade changes, pacing, duplicate submissions and a platform outage. There should be no separate math activity at login or unlock.

## 3. Paw Post Typing — add an optional verifier

Provider: `peterholko.paw-post`. Proposed unit: **completed delivery**.

Paw Post currently evaluates lessons in its own QML/JavaScript frontend. That is enough for a practice game, but a client-supplied WPM or completed flag must not be sufficient to grant privileged screen time.

1. Add an optional backend that issues a lesson ID and text, records the start time, and validates the submitted result against that issued lesson. Choose age-appropriate minimum length, accuracy and plausible elapsed-time requirements for a completed delivery; keep them in the game.
2. Bind the challenge to the Unix caller and reject reused, abandoned or expired lesson IDs. Count only input for the current game lesson; do not install a system-wide keyboard logger or collect unrelated keystrokes.
3. Persist a completion receipt and submit it through the SDK. The frontend displays the actual credited seconds after acknowledgement, alongside its existing delivery reward.
4. Continue existing typing play when the verifier or platform is absent. Time rewards start disabled and cannot be claimed from self-reported frontend statistics.
5. Test partial lessons, low accuracy, implausible durations, repeated delivery requests, restarts, lost replies, daily caps and ordinary play without integration.

An issued challenge and pacing checks discourage trivial reward-button abuse; they cannot prove that a human typed the text on a child-controlled client. Treat typing rewards as supervised practice, not a cheat-proof assessment.

## Rollout and compatibility

Updates ship in each game's individual plugin repository, with focused backend tests and local UI verification. Keep Screen Time free of game-specific imports and practice content. Omarchy Kids is outside this rollout.

During the transition, an explicit per-game backend selection may retain compatibility with the old combined plugin. Pin that choice into each pending receipt. Never send one completion to both backends, and never move an unresolved receipt to the other backend after a timeout.

Use one active time enforcer per child account. Moving to this platform requires an explicit enrollment change; it does not automatically copy old budgets, daily balances or the old PIN. Prepare and review a separate migration if those settings need to carry over. Root-only provider registration and parent opt-in follow enrollment. Do not enable rewards automatically during an update.

Acceptance requires: the game works alone; a child cannot call a generic grant endpoint; the parent controls rates/caps; a genuine completion credits once across retries; all games share the same overall cap; blocked/paused/Agreement sessions produce no credits; and the combined School & Screen Time installation remains usable until a parent deliberately migrates.
