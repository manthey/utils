#!/usr/bin/env bash
# Install Claude Code with the native installer. The binary lands in
# ~/.local/share/claude/versions/ with a launcher at ~/.local/bin/claude, so
# nothing it needs lives in ~/.claude.
#
# Usage: install_claude.sh [install|update]
#   install (default)  install Claude Code and copy the persisted state from
#                      ~/.claude-persist into ~/.claude and ~/.claude.json
#   update             merge or copy the state that changed in the sandbox
#                      back to ~/.claude-persist, and put just the changed
#                      files in ~/.claude-outbox (the host's claudeout then
#                      copies that folder out)
#
# Supported: linux, macOS, and windows under Git Bash (the agent sandbox).
set -euo pipefail

# ~/.claude-persist is a copy of the host's folder, made by the agent script
# before this runs; nothing is mounted or linked. Only these entries of
# ~/.claude are carried, plus the login (.credentials.json) and
# ~/.claude.json (onboarding, project trust, /config choices).
PERSIST="$HOME/.claude-persist"
ENTRIES=(CLAUDE.md settings.json rules projects history.jsonl .credentials.json)
# Several sandboxes can run at once from the same host folder, so update
# merges the files every session changes instead of replacing them: the
# prompt history is the union of both sides' lines, and the two settings
# files get a three-way merge where a key changed in the sandbox wins, a key
# changed only on the host is kept, and lists (permissions) are unioned.
# ~/.claude-base holds the sandbox's copies as of the last install or update
# and is the base of those merges. The host's claudeout refreshes
# ~/.claude-persist from the host folder before running update, so the
# merge sees what other sandboxes have saved since; without that refresh
# the merge just reproduces the sandbox's files. Everything else (CLAUDE.md,
# rules, projects, the login) is copied when the sandbox's file is newer.
BASE="$HOME/.claude-base"
MERGED=(history.jsonl settings.json)
# Set by update: every file copied into $PERSIST is also put here, so the host
# only has to copy what changed.
OUTBOX=""

# Put a copy of a file just copied into $PERSIST in $OUTBOX, at the same
# relative path.
stage() {
  [ -n "$OUTBOX" ] || return 0
  local rel="${1#"$PERSIST"/}"
  mkdir -p "$OUTBOX/$(dirname "$rel")"
  cp -p "$1" "$OUTBOX/$rel"
}

# Copy a file when the source is newer than the destination (or the
# destination is missing), or always when a third argument "force" is given.
# cp -p keeps the mtime, so a file copied in at install is not copied back
# out unless the sandbox changed it.
copy_newer() {
  local s="$1" d="$2" force="${3:-}"
  if [ -s "$s" ] && { [ -n "$force" ] || [ ! -e "$d" ] || [ "$s" -nt "$d" ]; }; then
    (umask 077; cp -p "$s" "$d")
    chmod 600 "$d"
    stage "$d"
    echo "Copied $(basename "$s") to $(dirname "$d")"
  fi
}

# Union of two prompt histories: the host's lines, plus the lines the sandbox
# added (in its history but not in the copy it started from), sorted by time.
# Lines the host dropped are not brought back.
merge_history() {
  local base="$1" mine="$2" theirs="$3"
  jq -nc --slurpfile base "$base" --slurpfile mine "$mine" --slurpfile theirs "$theirs" \
    '$theirs + ($mine - $base) | unique | sort_by(.timestamp? // 0) | .[]'
}

# Three-way merge of two settings files (JSON objects), key by key: a key
# only one side changed takes that side's value (including a deletion), a
# key both changed takes the sandbox's, nested objects recurse, and lists
# become the union minus what either side removed.
merge_settings() {
  local base="$1" mine="$2" theirs="$3"
  jq -n --slurpfile base "$base" --slurpfile mine "$mine" --slurpfile theirs "$theirs" '
    def merge3($b; $m; $t):
      if $m == $t then $m
      elif $m == $b then $t
      elif $t == $b then $m
      elif ($m | type) == "object" and ($t | type) == "object" then
        ($b | if type == "object" then . else {} end) as $bo
        | reduce (($t + $m) | keys_unsorted[]) as $k ({};
            if ($m | has($k)) and ($t | has($k)) then
              .[$k] = merge3($bo[$k]; $m[$k]; $t[$k])
            elif ($m | has($k)) then
              if ($bo | has($k)) and $m[$k] == $bo[$k] then . else .[$k] = $m[$k] end
            else
              if ($bo | has($k)) and $t[$k] == $bo[$k] then . else .[$k] = $t[$k] end
            end)
      elif ($m | type) == "array" and ($t | type) == "array" then
        ($b | if type == "array" then . else [] end) as $ba
        | reduce ($t + $m)[] as $x ([]; if any(.[]; . == $x) then . else . + [$x] end)
        | . - ($ba - $m) - ($ba - $t)
      else $m end;
    merge3($base[0]; $mine[0]; $theirs[0])'
}

# Merge the sandbox's copy of a file ($2) into the persisted one ($3) with
# merge_$1, using the copy in $BASE as the base, and stage the result when it
# differs from what was persisted. If the files cannot be parsed, fall back
# to copying the sandbox's file when it is newer.
merge_file() {
  local kind="$1" mine="$2" theirs="$3" base tmp
  base="$BASE/$(basename "$3")"
  [ -s "$mine" ] || return 0
  [ -s "$base" ] || base=/dev/null
  [ -s "$theirs" ] || theirs=/dev/null
  tmp="$(mktemp)"
  if ! command -v jq >/dev/null 2>&1; then
    rm -f "$tmp"
    echo "jq is not installed, so $(basename "$3") cannot be merged; copying it instead" >&2
    copy_newer "$mine" "$3"
    return 0
  fi
  if ! "merge_$kind" "$base" "$mine" "$theirs" > "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    echo "Could not merge $(basename "$3"); copying it instead" >&2
    copy_newer "$mine" "$3"
    return 0
  fi
  if ! cmp -s "$tmp" "$theirs"; then
    (umask 077; cp "$tmp" "$3")
    chmod 600 "$3"
    stage "$3"
    echo "Merged $(basename "$3") into $(dirname "$3")"
  fi
  rm -f "$tmp"
}

# Record the sandbox's current copies of the merged files as the base for the
# next update.
save_base() {
  local f
  rm -rf "$BASE"
  (umask 077; mkdir -p "$BASE")
  for f in "${MERGED[@]/#/$HOME/.claude/}" "$HOME/.claude.json"; do
    [ -s "$f" ] && cp -p "$f" "$BASE/$(basename "$f")"
  done
  return 0
}

# Copy each entry from one ~/.claude-like folder to another; directories are
# merged. A third argument "force" copies files even when they are older;
# without it, only files in a directory that are new or newer are copied,
# and the files in MERGED are merged rather than copied.
copy_entries() {
  local from="$1" to="$2" force="${3:-}" entry
  mkdir -p "$to"
  for entry in "${ENTRIES[@]}"; do
    # Already the same file (linked by an older version of this script).
    if [ "$from/$entry" -ef "$to/$entry" ]; then
      continue
    elif [ -z "$force" ] && [[ " ${MERGED[*]} " == *" $entry "* ]]; then
      merge_file "${entry%.*}" "$from/$entry" "$to/$entry"
    elif [ -d "$from/$entry" ] && [ -n "$force" ]; then
      mkdir -p "$to/$entry"
      cp -a "$from/$entry/." "$to/$entry/"
    elif [ -d "$from/$entry" ]; then
      (cd "$from" && find "$entry" -type f) | while IFS= read -r f; do
        if [ ! -e "$to/$f" ] || [ "$from/$f" -nt "$to/$f" ]; then
          mkdir -p "$to/$(dirname "$f")"
          cp -p "$from/$f" "$to/$f"
          stage "$to/$f"
        fi
      done
    elif [ -f "$from/$entry" ]; then
      copy_newer "$from/$entry" "$to/$entry" "$force"
    fi
  done
}

# Set hasCompletedOnboarding in ~/.claude.json with jq, or python3 when jq is
# not installed; without either, add the key after the object's opening brace.
set_onboarded() {
  local f="$HOME/.claude.json" ok=""
  [ -s "$f" ] || echo '{}' > "$f"
  if command -v jq >/dev/null 2>&1; then
    jq '.hasCompletedOnboarding = true' "$f" > "$f.tmp" && ok=1
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
d["hasCompletedOnboarding"] = True
json.dump(d, sys.stdout, indent=2)' "$f" > "$f.tmp" && ok=1
  elif [ -z "$(tr -d '{} \t\r\n' < "$f")" ]; then
    echo '{"hasCompletedOnboarding": true}' > "$f.tmp" && ok=1
  elif grep -q '"hasCompletedOnboarding": *true' "$f"; then
    return 0
  elif [ "$(head -c 1 "$f")" = "{" ]; then
    # A JSON object: add the key right after its opening brace.
    sed '1s/^{/{"hasCompletedOnboarding": true,/' "$f" > "$f.tmp" && ok=1
  fi
  if [ -n "$ok" ]; then
    mv "$f.tmp" "$f"
  else
    rm -f "$f.tmp"
    echo "Could not mark onboarding complete in $f (needs jq or python3)" >&2
  fi
}

case "${1:-install}" in
  install) ;;
  update)
    OUTBOX="$HOME/.claude-outbox"
    rm -rf "$OUTBOX"
    (umask 077; mkdir -p "$OUTBOX")
    copy_entries "$HOME/.claude" "$PERSIST"
    merge_file settings "$HOME/.claude.json" "$PERSIST/.claude.json"
    save_base
    exit 0
    ;;
  *) echo "Usage: $0 [install|update]" >&2; exit 2 ;;
esac

BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
# Whether the shell that ran this already finds commands in $BINDIR; if not,
# the profiles get a line that adds it.
case ":$PATH:" in
  *":$BINDIR:"*) BINDIR_ON_PATH=1 ;;
  *) BINDIR_ON_PATH="" ;;
esac
# Optional channel or version: "stable", "latest" (default), or e.g. "2.1.89".
CLAUDE_CHANNEL="${CLAUDE_CHANNEL:-}"

# Download a URL to stdout with curl, or wget when curl is not installed.
fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$1"
  else
    echo "ERROR: neither curl nor wget is installed" >&2
    return 1
  fi
}

case "$(uname -s)" in
MINGW*|MSYS*|CYGWIN*)
  # install.sh refuses Git Bash; the PowerShell installer puts claude.exe in
  # %USERPROFILE%\.local\bin, which is $BINDIR here.
  installer="$(mktemp -d)/install.ps1"
  fetch https://claude.ai/install.ps1 > "$installer"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$installer")" \
    ${CLAUDE_CHANNEL:+"$CLAUDE_CHANNEL"}
  rm -rf "$(dirname "$installer")"
  ;;
*)
  fetch https://claude.ai/install.sh | bash -s ${CLAUDE_CHANNEL:+"$CLAUDE_CHANNEL"}
  ;;
esac

# The persisted copies win over anything the installer already wrote there.
copy_entries "$PERSIST" "$HOME/.claude" force
# The persisted ~/.claude.json replaces the (newer) one the installer wrote.
if [ -s "$PERSIST/.claude.json" ]; then
  (umask 077; cp -p "$PERSIST/.claude.json" "$HOME/.claude.json")
  chmod 600 "$HOME/.claude.json"
fi

# At shell start, as an alternative to the copied login, use a
# `claude setup-token` token if one is saved. zsh is the default shell on
# macOS; the same lines go in its profile.
PROFILES=("$HOME/.bashrc")
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = zsh ]; then
  PROFILES+=("${ZDOTDIR:-$HOME}/.zshrc")
fi
for PROFILE in "${PROFILES[@]}"; do
  [ -f "$PROFILE" ] || touch "$PROFILE"
  grep -qF '.claude-persist/oauth_token' "$PROFILE" || cat <<'EOF' >> "$PROFILE"
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -s "$HOME/.claude-persist/oauth_token" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$HOME/.claude-persist/oauth_token")"
  export CLAUDE_CODE_OAUTH_TOKEN
fi
EOF
  grep -qF 'DISABLE_AUTOUPDATER=1' "$PROFILE" || echo 'export DISABLE_AUTOUPDATER=1' >> "$PROFILE"
  if [ -z "$BINDIR_ON_PATH" ] && ! grep -qF "# claude bindir" "$PROFILE"; then
    printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac  # claude bindir\n' \
      "$BINDIR" "$BINDIR" >> "$PROFILE"
  fi
done

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) PATH="$BINDIR:$PATH" ;;
esac

"$BINDIR/claude" --version >/dev/null 2>&1 || {
  echo "ERROR: claude did not run successfully; review the installer output above" >&2
  exit 1
}
# Mark onboarding complete so a fresh sandbox skips the first-run screens
# even before a copy has been persisted.
set_onboarded
save_base

echo "Done. $("$BINDIR/claude" --version)"
if [ -z "$BINDIR_ON_PATH" ]; then
  echo "$BINDIR was not on PATH; it was added to ${PROFILES[*]}. In this shell, run:"
  echo "  export PATH=\"$BINDIR:\$PATH\""
fi
