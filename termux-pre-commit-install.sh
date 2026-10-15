#!/data/data/com.termux/files/usr/bin/bash
#
# termux-pre-commit-install.sh
#
# Prepare a fresh Termux environment so this repo's pre-commit setup
# (including the ruff-pre-commit hook) works out of the box.
#
# Why this exists: the ruff-pre-commit hook declares "ruff==<ver>" as a pip
# dependency. PyPI publishes no wheels compatible with Termux's Python
# (Android/bionic), so pre-commit's pip install of ruff fails on Termux.
# This script builds a tiny local "stub" wheel that pip accepts; its only
# job is to exec the Termux-packaged ruff binary. Nothing in the repo
# changes: other machines keep installing ruff from PyPI because they never
# set PIP_FIND_LINKS.
#
# What it does (idempotent, safe to re-run):
#   1. pkg-install python, python-pip, git, ruff (only what is missing)
#   2. pip-install pre-commit, setuptools, wheel
#   3. determine the ruff version pinned by the ruff-pre-commit rev in
#      .pre-commit-config.yaml (fetches that rev's pyproject.toml from
#      GitHub), or take it from --ruff-version
#   4. build the stub wheel ruff-<ver>-py3-none-any.whl (skipped if present)
#   5. persist PIP_FIND_LINKS in ~/.bashrc (and ~/.zshrc if it exists)
#   6. run "pre-commit install" when the cwd is a git repo with pre-commit
#      config
#
# Usage:
#   ./termux-pre-commit-install.sh [options]
# Options:
#   --ruff-version X.Y.Z   skip the GitHub lookup and pin this version
#   --config PATH          pre-commit config to read the rev from
#                          (default: .pre-commit-config.yaml)
#   --no-hook-install      do not run "pre-commit install"
#   --skip-update          do not run "pkg update" first
#
# Note: the first "pre-commit run" afterwards still clones hook repositories
# and builds their environments (one-time, cached in ~/.cache/pre-commit).

set -euo pipefail

WHEEL_DIR="$HOME/.local/share/ruff-termux-wheels"
STUB_DIR="$WHEEL_DIR/ruff-stub"
CONFIG=".pre-commit-config.yaml"
RUFF_VERSION=""
HOOK_INSTALL=1
PKG_UPDATE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ruff-version) RUFF_VERSION="${2:?missing value}"; shift 2 ;;
    --config) CONFIG="${2:?missing value}"; shift 2 ;;
    --no-hook-install) HOOK_INSTALL=0; shift ;;
    --skip-update) PKG_UPDATE=0; shift ;;
    -h|--help) grep '^#' "$0" | tail -n +2 | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [[ "${PREFIX:-}" != *com.termux* ]]; then
  echo "error: this script must run inside Termux (PREFIX=${PREFIX:-unset})" >&2
  exit 1
fi

echo "==> installing pkg dependencies (missing only)"
if [[ $PKG_UPDATE -eq 1 ]]; then
  pkg update -y >/dev/null 2>&1 || echo "    pkg update failed, continuing" >&2
fi
for p in python python-pip git ruff; do
  if ! dpkg -s "$p" >/dev/null 2>&1; then
    echo "    pkg install $p"
    pkg install -y "$p"
  fi
done

echo "==> pip-installing pre-commit, setuptools, wheel"
python -m pip install -q pre-commit setuptools wheel
echo "    $(pre-commit --version)"

echo "==> determining pinned ruff version"
if [[ -z $RUFF_VERSION ]]; then
  if [[ ! -f $CONFIG ]]; then
    echo "error: $CONFIG not found; pass --config PATH or --ruff-version X.Y.Z" >&2
    exit 1
  fi
  rev=$(awk '/ruff-pre-commit/{f=1; next} f && /rev:/{sub(/^[ \t]*rev:[ \t]*/, ""); print; exit}' "$CONFIG")
  if [[ -z $rev ]]; then
    echo "error: could not find the ruff-pre-commit rev in $CONFIG" >&2
    exit 1
  fi
  echo "    ruff-pre-commit rev: $rev"
  pinned=$(python - "$rev" <<'PY'
import re
import sys
import urllib.request

rev = sys.argv[1]
url = ("https://raw.githubusercontent.com/astral-sh/ruff-pre-commit/"
       f"{rev}/pyproject.toml")
try:
    text = urllib.request.urlopen(url, timeout=30).read().decode()
except OSError as e:
    print(f"error: cannot fetch {url}: {e}", file=sys.stderr)
    sys.exit(1)
m = re.search(r"ruff==([0-9.]+)", text)
print(m.group(1) if m else "")
PY
) || exit 1
  if [[ -z $pinned ]]; then
    echo "error: no 'ruff==' dependency found in the pyproject of $rev" >&2
    exit 1
  fi
  RUFF_VERSION=$pinned
fi
echo "    pinned ruff version: $RUFF_VERSION"

echo "==> building stub wheel ruff-$RUFF_VERSION-py3-none-any.whl"
mkdir -p "$STUB_DIR/ruff_stub"
cat > "$STUB_DIR/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "ruff"
version = "$RUFF_VERSION"
description = "Stub: delegates to the Termux-packaged ruff binary"

[project.scripts]
ruff = "ruff_stub:main"

[tool.setuptools]
packages = ["ruff_stub"]
EOF
cat > "$STUB_DIR/ruff_stub/__init__.py" <<'EOF'
import os
import shutil
import sys


def main():
    # Never resolve to our own venv: that would exec this stub recursively.
    path = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not os.path.abspath(p).startswith(os.path.abspath(sys.prefix))
    )
    system = shutil.which("ruff", path=path)
    if not system:
        sys.stderr.write("ruff stub: no system ruff found on PATH\n")
        raise SystemExit(127)
    os.execv(system, ["ruff"] + sys.argv[1:])
EOF
if [[ -f "$WHEEL_DIR/ruff-$RUFF_VERSION-py3-none-any.whl" ]]; then
  echo "    wheel already present, skipping build"
else
  (cd "$STUB_DIR" && python -m pip wheel . --no-deps --no-build-isolation -w "$WHEEL_DIR" -q)
  echo "    built: $WHEEL_DIR/ruff-$RUFF_VERSION-py3-none-any.whl"
fi

echo "==> persisting PIP_FIND_LINKS"
if [[ -n "${PIP_FIND_LINKS:-}" && "${PIP_FIND_LINKS}" != *"ruff-termux-wheels"* ]]; then
  echo "warning: PIP_FIND_LINKS is already set to '$PIP_FIND_LINKS' and does" >&2
  echo "         not include $WHEEL_DIR; add it manually to your shell rc." >&2
fi
line='export PIP_FIND_LINKS=$HOME/.local/share/ruff-termux-wheels'
[[ -f $HOME/.bashrc ]] || : > "$HOME/.bashrc"
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [[ -f $rc ]] || continue
  if grep -qF 'ruff-termux-wheels' "$rc"; then
    echo "    already present in $rc"
  else
    printf '\n# termux ruff stub wheels for pre-commit\n%s\n' "$line" >> "$rc"
    echo "    added to $rc"
  fi
done
export PIP_FIND_LINKS="$WHEEL_DIR"

echo "==> sanity check: pip resolves the stub wheel"
if python -m pip install --dry-run "ruff==$RUFF_VERSION" 2>/dev/null | grep -q "Would install ruff-$RUFF_VERSION"; then
  echo "    ok"
else
  echo "warning: pip did not resolve the stub wheel; pre-commit's ruff hook" >&2
  echo "         will still fail until PIP_FIND_LINKS is visible to it." >&2
fi

if [[ $HOOK_INSTALL -eq 1 && -f $CONFIG && -d .git ]]; then
  echo "==> running pre-commit install"
  pre-commit install
fi

echo "==> done. ruff on PATH: $(command -v ruff) ($(ruff --version))"
echo "    next: cd into your repo and run 'pre-commit run -a' (first run"
echo "    clones hook repos and may take a few minutes)."
echo "export PIP_FIND_LINKS=$PIP_FIND_LINKS"
