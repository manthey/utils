#!/usr/bin/env bash
# Install Claude Code with the native installer. The binary lands in
# ~/.local/share/claude/versions/ with a launcher at ~/.local/bin/claude, so
# nothing it needs lives in ~/.claude.
#
# Usage: install_claude.sh [install|update]
#   install (default)  install Claude Code and copy the persisted state from
#                      ~/.claude-persist into ~/.claude and ~/.claude.json
#   update             copy the state that changed in the sandbox back to
#                      ~/.claude-persist, and put just the changed files in
#                      ~/.claude-outbox (the host's claudeout then copies
#                      that folder out)
#
# Supported: linux, macOS, and windows under Git Bash (the agent sandbox).
set -euo pipefail

# ~/.claude-persist is a copy of the host's folder, made by the agent script
# before this runs; nothing is mounted or linked. Only these entries of
# ~/.claude are carried, plus the login (.credentials.json) and
# ~/.claude.json (onboarding, project trust, /config choices).
PERSIST="$HOME/.claude-persist"
ENTRIES=(CLAUDE.md settings.json rules projects .credentials.json)
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

# Copy each entry from one ~/.claude-like folder to another; directories are
# merged. A third argument "force" copies files even when they are older;
# without it, only files in a directory that are new or newer are copied.
copy_entries() {
  local from="$1" to="$2" force="${3:-}" entry
  mkdir -p "$to"
  for entry in "${ENTRIES[@]}"; do
    # Already the same file (linked by an older version of this script).
    if [ "$from/$entry" -ef "$to/$entry" ]; then
      continue
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

case "${1:-install}" in
  install) ;;
  update)
    OUTBOX="$HOME/.claude-outbox"
    rm -rf "$OUTBOX"
    (umask 077; mkdir -p "$OUTBOX")
    copy_entries "$HOME/.claude" "$PERSIST"
    copy_newer "$HOME/.claude.json" "$PERSIST/.claude.json"
    exit 0
    ;;
  *) echo "Usage: $0 [install|update]" >&2; exit 2 ;;
esac

BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
# Optional channel or version: "stable", "latest" (default), or e.g. "2.1.89".
CLAUDE_CHANNEL="${CLAUDE_CHANNEL:-}"

case "$(uname -s)" in
MINGW*|MSYS*|CYGWIN*)
  # install.sh refuses Git Bash; the PowerShell installer puts claude.exe in
  # %USERPROFILE%\.local\bin, which is $BINDIR here.
  installer="$(mktemp -d)/install.ps1"
  curl -fsSL https://claude.ai/install.ps1 -o "$installer"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$installer")" \
    ${CLAUDE_CHANNEL:+"$CLAUDE_CHANNEL"}
  rm -rf "$(dirname "$installer")"
  ;;
*)
  curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CHANNEL:+"$CLAUDE_CHANNEL"}
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
[ -s "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"
jq '.hasCompletedOnboarding = true' "$HOME/.claude.json" > "$HOME/.claude.json.tmp" &&
  mv "$HOME/.claude.json.tmp" "$HOME/.claude.json"

echo "Done. $("$BINDIR/claude" --version)"
