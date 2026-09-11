#!/bin/bash

# omarchy:summary=Shared functions for the screen time daemon, client and parent command
# omarchy:hidden=true

# Sourced, not run. Everything the four screen time scripts share: where the
# files live, what a config and a day look like, and the status the
# widget reads. Nothing here talks to a terminal; callers print.
#
# The three roots can be overridden for tests, which run the daemon against a
# temp directory and a fake loginctl on PATH instead of a real machine.
ST_ETC="${PETERHOLKO_SCREEN_TIME_ETC:-/etc/peterholko-screen-time}"
ST_STATE="${PETERHOLKO_SCREEN_TIME_STATE:-/var/lib/peterholko-screen-time}"
ST_RUN="${PETERHOLKO_SCREEN_TIME_RUN:-/run/peterholko-screen-time}"
ST_CONFIG="$ST_ETC/config.json"
ST_LOCK="$ST_RUN/lock"
ST_GROUP="peterholko-screen-time"
ST_SERVICE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

ST_TICK=5
ST_REST_RESET=300 # five quiet minutes and a stretch starts over
ST_REFLECTIONS=20
ST_HISTORY_DAYS=14
ST_LOCK_FAILURES_MAX=3 # locks in a row the shell fails before root ends the session
# The defaults, and the rules that clamp whatever is on disk to something the
# daemon can run on. A profile with a negative budget or a string where a
# number belongs must not stop the daemon, because a daemon that is not
# running is unlimited screen time.
read -r -d '' ST_JQ_LIB <<'JQ' || true
def days: ["mon","tue","wed","thu","fri","sat","sun"];
def default_profile: {"name": "Default", "philosophy": "limits", "respect_school_mode": false, "budget_minutes": {"mon": 60, "tue": 60, "wed": 60, "thu": 60, "fri": 60, "sat": 90, "sun": 90}, "blocked_periods": [{"label": "Bedtime", "enabled": false, "start": "20:00", "end": "07:00"}], "warn_minutes": [15, 5, 1], "on_empty": "lock", "grace_seconds": 60, "relock_seconds": 30, "unlock_grace_seconds": 120, "agreement_text": "", "agreement_minutes": 0, "break_nudge_minutes": 45, "credits": {"enabled": false, "daily_cap_minutes": 30, "providers": {}}};
def default_config: {version: 2, authentication: {pin_enabled: false, pin: null}, active_profile: "default", profiles: {default: default_profile}, users: {}, providers: {}};

def clampint($fallback; $low; $high):
  if type == "number" then (floor | if . < $low then $low elif . > $high then $high else . end)
  elif type == "string" and test("^-?[0-9]+$") then (tonumber | clampint($fallback; $low; $high))
  else $fallback end;
def clampnum($fallback; $low; $high):
  if type == "number" then (if . < $low then $low elif . > $high then $high else . end)
  else $fallback end;
def time_of_day($fallback):
  if type == "string" and test("^([01]?[0-9]|2[0-3]):[0-5]?[0-9]$")
  then (split(":") | map(tonumber) | "\(.[0] | tostring | if length < 2 then "0" + . else . end):\(.[1] | tostring | if length < 2 then "0" + . else . end)")
  else $fallback end;

def sanitize_credits:
  (if type == "object" then . else {} end) as $raw |
  {enabled: ($raw.enabled == true), daily_cap_minutes: ($raw.daily_cap_minutes | clampint(30; 0; 1440)),
   providers: (($raw.providers // {}) | if type == "object" then . else {} end |
     with_entries(select(.key | test("^[a-z0-9][a-z0-9.-]{1,95}$"))) |
     map_values({enabled: (.enabled == true), seconds_per_event: (.seconds_per_event | clampint(60; 1; 3600)),
                 daily_cap_minutes: (.daily_cap_minutes | clampint(10; 0; 1440))}))};

def sanitize_periods:
  default_profile.blocked_periods[0] as $d |
  (if type == "array" then . else [$d] end) |
  [ .[:8][] | select(type == "object") |
    {label: ((.label // "" | tostring | .[:40]) as $l | if ($l | test("^\\s*$")) then "Blocked" else ($l | sub("^\\s+"; "") | sub("\\s+$"; "")) end),
     enabled: (.enabled == true),
     start: (.start | time_of_day($d.start)),
     end: (.end | time_of_day($d.end))}
    | select(.start != .end) ];

def sanitize_profile:
  (if type == "object" then . else {} end) as $raw | default_profile as $d |
  {
    name: (($raw.name // $d.name) | tostring | .[:40]),
    philosophy: (if ($raw.philosophy == "together") then "together" else "limits" end),
    respect_school_mode: ($raw.respect_school_mode == true),
    agreement_text: (($raw.agreement_text // "") | tostring | .[:500]),
    agreement_minutes: ($raw.agreement_minutes | clampint($d.agreement_minutes; 0; 1440)),
    break_nudge_minutes: ($raw.break_nudge_minutes | clampint($d.break_nudge_minutes; 0; 480)),
    budget_minutes: (($raw.budget_minutes // {}) as $b | reduce days[] as $day ({}; .[$day] = ($b[$day] | clampint($d.budget_minutes[$day]; 0; 1440)))),
    blocked_periods: ($raw.blocked_periods | sanitize_periods),
    warn_minutes: ([($raw.warn_minutes // $d.warn_minutes)[]? | select(type == "number") | floor | select(. > 0 and . <= 1440)] | unique | reverse | if length == 0 then $d.warn_minutes else . end),
    on_empty: (if $raw.on_empty == "notify" then "notify" else "lock" end),
    grace_seconds: ($raw.grace_seconds | clampint($d.grace_seconds; 0; 3600)),
    relock_seconds: ($raw.relock_seconds | clampint($d.relock_seconds; 5; 3600)),
    unlock_grace_seconds: ($raw.unlock_grace_seconds | clampint($d.unlock_grace_seconds; 5; 3600)),
    credits: ($raw.credits | sanitize_credits)
  };

def sanitize_config:
  (if type == "object" then . else {} end) as $raw |
  (($raw.profiles // {}) | if type == "object" then . else {} end
    | with_entries(select(.key | test("^\\S.{0,39}$"))) | map_values(sanitize_profile)
    | if length == 0 then {default: (null | sanitize_profile)} else . end) as $profiles |
  (if ($profiles | has($raw.active_profile // "")) then $raw.active_profile else ($profiles | keys_unsorted[0]) end) as $active |
  {
    version: 2,
    authentication: ($raw.authentication // {pin_enabled: false, pin: null}),
    providers: ($raw.providers // {}),
    active_profile: $active,
    profiles: $profiles,
    users: (($raw.users // {}) | if type == "object" then . else {} end
      | with_entries(select(.key | test("^[a-z_][a-z0-9_-]{0,31}$")))
      | map_values(. as $entry | {profile: (if (type == "object") and ($profiles | has($entry.profile // "")) then $entry.profile else $active end)}))
  };

def profile_for($user):
  (.users[$user].profile // .active_profile) as $key |
  (if (.profiles | has($key)) then $key else .active_profile end) as $key |
  {key: $key, profile: .profiles[$key]};

# Deep merge for `config patch`: objects recurse, everything else is replaced.
def deep_merge($patch):
  reduce ($patch | to_entries[]) as $e (.;
    if ($e.value | type) == "object" and ((.[$e.key] // null) | type) == "object"
    then .[$e.key] = (.[$e.key] | deep_merge($e.value))
    else .[$e.key] = $e.value end);

def new_day($day; $budget; $profile): {
  day: $day, profile: $profile, budget_seconds: $budget,
  spent_seconds: 0, credited_seconds: 0, granted_seconds: 0,
  credit_totals: {}, credit_receipts: {},
  warned: [], agreement_noted: false, ledger: []
};
def day_total: .budget_seconds + .credited_seconds + .granted_seconds;
def day_remaining: day_total - .spent_seconds;
def day_summary: {day, budget_seconds, spent_seconds, credited_seconds, granted_seconds};
JQ

st_jq() {
  # jq with the shared definitions in front. Callers pass their own filter
  # last, after any --arg/--argjson, exactly as with jq itself.
  local args=() filter
  while (($# > 1)); do args+=("$1"); shift; done
  filter=$1
  jq "${args[@]}" "$ST_JQ_LIB $filter"
}

# time -------------------------------------------------------------------

st_now() { date +%s; }
st_uptime() { cut -d' ' -f1 /proc/uptime | cut -d. -f1; }
st_day_key() { date -d "@${1:-$(st_now)}" +%F; }
st_weekday_key() { date -d "@${1:-$(st_now)}" +%a | tr '[:upper:]' '[:lower:]'; }
st_moment() { date -d "@${1:-$(st_now)}" +%H:%M; }

st_human_time() {
  # 5400 -> 1h30, 3600 -> 1h, 900 -> 15m. Whole hours read as "1h"; the
  # zeroes only earn their place when there are minutes next to them.
  local s=${1:-0} h m
  ((s < 0)) && s=0
  if ((s >= 3600)); then
    h=$((s / 3600)); m=$((s % 3600 / 60))
    if ((m == 0)); then echo "${h}h"; else printf '%dh%02d\n' "$h" "$m"; fi
  else
    echo "$((s / 60))m"
  fi
}

# files ------------------------------------------------------------------

st_write_private() {
  # Write through a temp file in the same directory and rename over the
  # destination: a plain `>` follows a symlink sitting on the path, rename(2)
  # replaces the path itself. Mode 0600, owner as given (default: root).
  # The daemon must retain the credit API's durability when rewriting the
  # same daily file, including its idempotency receipts.
  python3 -I -c '
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import write
write(Path(sys.argv[2]), json.load(sys.stdin))
' "$(dirname "${BASH_SOURCE[0]}")" "$1"
}

st_read_regular() {
  # A file, refusing anything that is not a plain regular file.
  [[ -f $1 && ! -L $1 ]] || return 1
  cat "$1"
}

st_private_dir() {
  # Make (or repair) a directory only its owner may enter.
  local path=$1 mode=${2:-0700}
  mkdir -p "$path"
  [[ -L $path ]] && { echo "screen-time: $path is a symlink" >&2; return 1; }
  chmod "$mode" "$path"
}

st_with_lock() {
  # Run a function while holding the state lock. The daemon and ctl both edit
  # days; whoever holds this is the only one doing it.
  mkdir -p "$ST_RUN"
  (
    flock -w 10 9 || { echo "screen-time: could not take the lock" >&2; exit 1; }
    "$@"
  ) 9>>"$ST_LOCK"
}

# config -----------------------------------------------------------------

st_config() {
  # The config as the daemon sees it: sanitized, defaults filled in.
  local raw
  raw=$(st_read_regular "$ST_CONFIG" 2>/dev/null) || raw='{}'
  st_jq -n --argjson raw "${raw:-null}" '$raw | sanitize_config' 2>/dev/null \
    || st_jq -n 'default_config | sanitize_config'
}

st_save_config() {
  # Sanitize what we were given and write it. Stdin is the config.
  st_private_dir "$ST_ETC" 0700 || return 1
  st_jq 'sanitize_config' | st_write_private "$ST_CONFIG"
}

st_managed_users() {
  # Account names in the roster that actually exist on the machine.
  local name
  while IFS= read -r name; do
    getent passwd "$name" >/dev/null && echo "$name"
  done < <(st_config | jq -r '.users | keys[]')
}

st_uid_of() { getent passwd "$1" | cut -d: -f3; }
st_gid_of() { getent passwd "$1" | cut -d: -f4; }
st_user_of() { getent passwd "$1" | cut -d: -f1; }
st_home_of() { getent passwd "$1" | cut -d: -f6; }

# per-account state ----------------------------------------------------------

st_user_state_dir() { echo "$ST_STATE/users/$1"; }
st_user_run_dir() { echo "$ST_RUN/$1"; }

st_day_path() { echo "$(st_user_state_dir "$1")/$2.json"; }

st_load_day() {
  # $1 uid, $2 day key, $3 budget seconds, $4 profile key. A missing or
  # unreadable day is a fresh one.
  local raw
  raw=$(st_read_regular "$(st_day_path "$1" "$2")" 2>/dev/null) || raw=''
  st_jq -n --arg day "$2" --argjson budget "$3" --arg profile "$4" --argjson raw "${raw:-null}" '
    if ($raw | type) == "object" and $raw.day == $day then
      new_day($day; $budget; $profile) + $raw | .budget_seconds = $budget | .profile = $profile
    else new_day($day; $budget; $profile) end'
}

st_save_day() {
  # $1 uid. Stdin: the day.
  st_private_dir "$(st_user_state_dir "$1")" || return 1
  local json
  json=$(cat)
  st_write_private "$(st_day_path "$1" "$(jq -r .day <<<"$json")")" <<<"$json"
}

st_load_json() {
  # $1 path, $2 fallback JSON. Any file that is not valid JSON is the fallback.
  local raw
  raw=$(st_read_regular "$1" 2>/dev/null) || raw=''
  if [[ -n $raw ]] && jq -e . <<<"$raw" >/dev/null 2>&1; then echo "$raw"; else echo "$2"; fi
}

st_runtime_default='{"paused":false,"stretch":0,"rest_since":null,"nudged":false,"lock_after":null,"lock_count":0,"lock_failures":0,"last_lock_ok":false,"blocked_since":null,"session":{"present":false,"active":false,"locked":false,"id":null}}'
st_load_runtime() { st_load_json "$(st_user_run_dir "$1")/runtime.json" "$st_runtime_default"; }
st_save_runtime() {
  # The daemon's between-ticks memory for one account. Root writes it; the
  # account may read it, and the widget does not need to.
  st_private_dir "$(st_user_run_dir "$1")" 0750 || return 1
  st_write_private "$(st_user_run_dir "$1")/runtime.json"
}

st_history() {
  # $1 uid: the last ST_HISTORY_DAYS days, oldest first, budget and spent per day.
  local uid=$1 dir i day
  dir=$(st_user_state_dir "$uid")
  for ((i = ST_HISTORY_DAYS - 1; i >= 0; i--)); do
    day=$(date -d "-$i day" +%F)
    st_load_json "$dir/$day.json" "{\"day\":\"$day\"}"
  done | st_jq -sc '[.[] | new_day(.day; 0; "") + . | day_summary]'
}

# what the widget reads --------------------------------------------------------

st_school_status() {
  # $1 enrolled username, $2 profile JSON, $3 current time. This reads the
  # separate School Mode service; it never installs or enables that service.
  if [[ $(jq -r '.respect_school_mode == true' <<<"$2") == "true" ]]; then
    /usr/bin/python3 -I "$ST_SERVICE_DIR/school.py" "$1" "$3" \
      || printf '%s\n' '{"linked":true,"available":false,"active":false,"reason":"unavailable"}'
  else
    printf '%s\n' '{"linked":false,"available":false,"active":false,"reason":"disabled"}'
  fi
}

st_publish() {
  # $1 uid, stdin: JSON. Written where the account itself can read it and
  # nobody can write it: the directory is root's, group the account's, 0750.
  local uid=$1 dir gid
  dir=$(st_user_run_dir "$uid")
  gid=$(st_gid_of "$uid")
  mkdir -p "$dir"
  chown "root:${gid:-0}" "$dir" 2>/dev/null || true
  chmod 0750 "$dir"
  local tmp
  tmp=$(mktemp "$dir/$2.XXXXXX") || return 1
  chmod 0640 "$tmp"
  chown "root:${gid:-0}" "$tmp" 2>/dev/null || true
  cat >"$tmp" && mv -f "$tmp" "$dir/$2"
}

st_status_json() {
  # The one JSON the pill, the panel and `omarchy-screen-time status` all
  # read. $1 uid, $2 username, $3 now, $4 profile key, $5 profile JSON,
  # $6 day JSON, $7 runtime JSON, $8 optional PIN enabled (true/false).
  local uid=$1 user=$2 now=$3 key=$4 profile=$5 day=$6 runtime=$7 pin_enabled=$8
  # One line: the widget's stream reads it line by line.
  st_jq -nc --arg user "$user" --argjson now "$now" --arg key "$key" \
    --argjson profile "$profile" --argjson day "$day" --argjson rt "$runtime" \
    --arg moment "$(st_moment "$now")" --argjson pin_enabled "$pin_enabled" '
    def covers($m): if .start == .end then false elif .start < .end then (.start <= $m and $m < .end) else ($m >= .start or $m < .end) end;
    ($profile.blocked_periods | map(select(.enabled))) as $enabled |
    ($enabled | map(select(covers($moment))) | .[0]) as $blocking |
    (($enabled | map(select(.start > $moment)) | sort_by(.start) | .[0]) // ($enabled | sort_by(.start) | .[0])) as $next |
    ($profile.philosophy == "together") as $together |
    ($rt.school_mode // {linked: false, available: false, active: false, reason: "disabled"}) as $school |
    ($profile.respect_school_mode == true and $school.linked == true and $school.available == true and $school.active == true) as $school_active |
    ($day | day_remaining) as $remaining |
    (if $rt.parent_lock_requested then "parent-lock" elif $together then null elif $blocking != null then "bedtime" elif $school_active then null elif $remaining <= 0 then "empty" else null end) as $reason |
    ($rt.session.present and $rt.session.active and ($rt.session.locked | not)) as $in_use |
    (if $rt.parent_lock_requested then "parent-lock" elif $rt.paused then "paused" elif $reason != null then $reason elif $school_active then "school" elif ($in_use | not) then "idle" else "running" end) as $phase |
    ($profile.credits.daily_cap_minutes * 60 - $day.credited_seconds | if . < 0 then 0 else . end) as $room |
    {
      ok: true, plugin_id: "peterholko.screen-time", api_version: 1, user: $user, profile: $key, profile_name: $profile.name,
      philosophy: $profile.philosophy, agreement_text: $profile.agreement_text,
      agreement_minutes: $profile.agreement_minutes, break_nudge_minutes: $profile.break_nudge_minutes,
      stretch_seconds: ($rt.stretch | floor),
      reflections: ([$day.ledger[] | select(.kind == "reflection") | {t: (.t // 0), text: ((.meta.text // "") | tostring)}] | .[-20:]),
      day: $day.day, phase: $phase, counting: ($phase == "running"), school_mode: $school,
      remaining_seconds: (if $remaining < 0 then 0 else $remaining end),
      budget_seconds: $day.budget_seconds, spent_seconds: $day.spent_seconds,
      credited_seconds: $day.credited_seconds, granted_seconds: $day.granted_seconds,
      warn_seconds: ($profile.warn_minutes | map(. * 60)),
      locked: $rt.session.locked, session_present: $rt.session.present,
      lock_in_seconds: (if ($rt.lock_after != null and $reason != null and (($rt.paused | not) or $rt.parent_lock_requested)) then ([$rt.lock_after - $now, 0] | max | floor) else null end),
      blocked_periods: $profile.blocked_periods,
      blocked_label: ($blocking.label // ""),
      next_block: $next,
      budget_minutes: $profile.budget_minutes,
      on_empty: $profile.on_empty,
      pin_enabled: $pin_enabled,
      credits: {
        enabled: ($profile.credits.enabled and ($school_active | not) and ($rt.parent_lock_requested | not)),
        cap_seconds: ($profile.credits.daily_cap_minutes * 60), room_seconds: $room,
        providers: ($profile.credits.providers | with_entries(.key as $id | .value += {credited_today_seconds: ($day.credit_totals[$id] // 0)})),
        events: ([$day.ledger[] | select(.kind == "credit") |
          {t: .t, seconds: .seconds, provider: .meta.provider, name: .meta.name}] | .[-50:])
      }
    }'
}

# the account's session -----------------------------------------------------------

st_omarchy_path() { printf '%s\n' "$OMARCHY_PATH"; }

st_user_env() {
  # The environment a command needs to reach the account's desktop from root:
  # its runtime dir and session bus, where the shell is, and nothing of ours.
  # A UTF-8 locale, or Qt (qs, behind omarchy-shell) writes a warning to the
  # journal on every tick.
  local uid=$1
  printf 'LANG=C.UTF-8\nXDG_RUNTIME_DIR=/run/user/%s\nDBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus\nHOME=%s\nOMARCHY_PATH=%s\nPATH=%s\n' \
    "$uid" "$uid" "$(st_home_of "$uid")" "$(st_omarchy_path)" \
    "${PETERHOLKO_SCREEN_TIME_PATH:-$(st_omarchy_path)/bin:/usr/local/bin:/usr/bin:/bin}"
}

st_as_user() {
  # Run a command as the account, in its session's environment, with a
  # timeout: a hung desktop must not hang the daemon. setpriv rather than
  # runuser: no PAM session, so nothing lands in the journal every tick.
  local uid=$1; shift
  local gid
  local session_env=()
  mapfile -t session_env < <(st_user_env "$uid")
  gid=$(st_gid_of "$uid") || return 1
  [[ -n $gid ]] || return 1
  if ((EUID == 0)); then
    timeout 5 setpriv --reuid="$uid" --regid="$gid" --init-groups env -i "${session_env[@]}" "$@"
  else
    # The tests run the daemon as themselves against a fake session.
    timeout 5 env -i "${session_env[@]}" "$@"
  fi
}

st_session_info() {
  # $1 uid. Prints "present active locked session_id" for the account's
  # graphical session, the active one first if there are several.
  #
  # Present and active come from logind and describe the seat, which the
  # account cannot talk logind into misreporting. Locked comes from the
  # shell, and only from the shell: logind's LockedHint is a hint the session
  # itself sets, so `busctl call ... SetLockedHint b true` from the account
  # would read as a locked screen and stop the clock. A shell that does not
  # answer counts as not locked, so freezing it buys nothing either.
  local uid=$1 ids id props type class active state
  local present=false is_active=false locked=false chosen=""
  ids=$(loginctl show-user "$uid" -p Sessions --value 2>/dev/null) || ids=""
  for id in $ids; do
    props=$(loginctl show-session "$id" -p Type -p Class -p Active -p State 2>/dev/null) || continue
    type=$(sed -n 's/^Type=//p' <<<"$props"); class=$(sed -n 's/^Class=//p' <<<"$props")
    [[ $class == user ]] || continue
    [[ $type == wayland || $type == x11 ]] || continue
    active=$(sed -n 's/^Active=//p' <<<"$props"); state=$(sed -n 's/^State=//p' <<<"$props")
    present=true
    if [[ -z $chosen || $active == yes ]]; then
      chosen=$id
      is_active=false; [[ $active == yes && ($state == active || $state == online) ]] && is_active=true
    fi
    [[ $active == yes ]] && break
  done
  if [[ $present == true ]]; then
    case "$(st_as_user "$uid" omarchy-shell lock isLocked 2>/dev/null | tr '[:upper:]' '[:lower:]')" in
      true) locked=true ;;
    esac
  fi
  echo "$present $is_active $locked $chosen"
}

st_notify() {
  # $1 uid, $2 title, $3 body, $4 urgency, $5 the id of the notification to
  # replace (empty for a new one). Prints the id the server gave, so the
  # caller can hand it back next time: one warning on screen, not a stack.
  local uid=$1 title=$2 body=$3 urgency=${4:-normal} replaces=${5:-}
  local args=(omarchy-notification-send -p --app-name "Screen Time" -u "$urgency")
  [[ $replaces =~ ^[0-9]+$ ]] && args+=(-r "$replaces")
  st_as_user "$uid" "${args[@]}" "$title" "$body" 2>/dev/null | tr -dc '0-9' || true
}

st_lock_session() {
  # $1 uid, $2 session id. Prints how it was done, or nothing on failure.
  # The shell's own lock, and nothing else: logind's lock signal is not
  # something the Quattro shell acts on, so `loginctl lock-session` succeeds
  # and locks nothing, and reporting that as a lock would hand a frozen or
  # killed shell an unlocked session for the unlock grace, over and over.
  # A lock that did not take is reported as exactly that; the daemon retries
  # and, when the shell keeps not answering, ends the session instead.
  # PETERHOLKO_SCREEN_TIME_LOCK_COMMAND replaces the whole thing for the tests,
  # which must prove the lock path without locking the tester out.
  local uid=$1 sid=$2
  if [[ -n ${PETERHOLKO_SCREEN_TIME_LOCK_COMMAND:-} ]]; then
    "$PETERHOLKO_SCREEN_TIME_LOCK_COMMAND" "$uid" "$sid" && echo "$PETERHOLKO_SCREEN_TIME_LOCK_COMMAND"
    return
  fi
  if st_as_user "$uid" omarchy-shell lock lock >/dev/null 2>&1; then
    local attempt
    for ((attempt = 0; attempt < 4; attempt++)); do
      if [[ $(st_as_user "$uid" omarchy-shell lock isLocked 2>/dev/null) == "true" ]]; then
        echo "omarchy-shell lock"
        return 0
      fi
      sleep 0.25
    done
  fi
  return 1
}

st_terminate_session() {
  # $1 uid, $2 session id. The lock the account cannot dodge: root ends the
  # session, which no process of the account's can prevent or fake. Reached
  # only after the shell has failed to lock several times in a row, so a
  # healthy machine never gets here. Prints how it was done, or nothing.
  #
  # The compositor goes first, the way a logout ends a session: SDDM's helper
  # then exits cleanly and the login screen comes back. `loginctl
  # terminate-session` kills that helper along with everything else, and SDDM
  # shows nothing afterwards, so it is only the fallback for a session with no
  # compositor to speak of.
  local uid=$1 sid=$2
  if [[ -n ${PETERHOLKO_SCREEN_TIME_TERMINATE_COMMAND:-} ]]; then
    "$PETERHOLKO_SCREEN_TIME_TERMINATE_COMMAND" "$uid" "$sid" && echo "$PETERHOLKO_SCREEN_TIME_TERMINATE_COMMAND"
    return
  fi
  # Never from a test run as a plain user: that would be the tester's own
  # compositor.
  ((EUID == 0)) || return 0
  if pkill -TERM -u "$uid" -x Hyprland 2>/dev/null; then
    sleep 3
    pkill -KILL -u "$uid" -x Hyprland 2>/dev/null || true
    echo "ending the compositor"
    return
  fi
  [[ -n $sid ]] && loginctl terminate-session "$sid" 2>/dev/null && echo "loginctl terminate-session"
}

st_poke_daemon() {
  # Ask the daemon to publish now rather than at its next tick. The pid file
  # is root's, but a pid can be reused after a crash, so the signal goes only
  # to a process that is actually the daemon.
  local pid
  pid=$(cat "$ST_RUN/pid" 2>/dev/null) || return 0
  [[ $pid =~ ^[0-9]+$ ]] || return 0
  grep -q peterholko-screen-time "/proc/$pid/cmdline" 2>/dev/null || return 0
  kill -USR1 "$pid" 2>/dev/null || true
}
