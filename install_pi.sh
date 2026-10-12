#!/usr/bin/env bash

# Standardize binary directory based on XDG spec or default fallback
export BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

# Node version pi requires. Keep this independent of whatever node the host
# container provides so pi can run even when no common version exists.
PI_NODE_VERSION="${PI_NODE_VERSION:-22}"

# Decide which NVM_DIR to use. We must not mutate the container's node setup:
# `nvm install`, `nvm alias default`, and repointing `$NVM_DIR/current` are all
# write operations. So we reuse the container's nvm ONLY when it is the same
# NVM_DIR, already has PI_NODE_VERSION installed, and already has default and
# `current` pointing at that version (i.e. all our writes would be no-ops).
# Otherwise we isolate into a private NVM_DIR and leave the container alone.
# This covers container node supplied by nvm, by a distro package, or by any
# other version manager.
PI_NVM_DIR_DEFAULT="${PI_NVM_DIR:-$HOME/.nvm-pi}"
SHARED_NVM_DIR="${NVM_DIR:-$HOME/.nvm}"

# Identify the nvm installation, if any, that owns the active node.
active_node="$(command -v node 2>/dev/null || true)"
active_real=""
[ -n "$active_node" ] && active_real="$(readlink -f "$active_node" 2>/dev/null || echo "$active_node")"
active_nvm=""
case "$active_real" in
  */versions/node/*/bin/node) active_nvm="${active_real%%/versions/node/*}" ;;
esac

REUSE_SHARED=0
if [ -z "$active_node" ]; then
  # No node at all: safe to use the shared nvm as the container has none.
  REUSE_SHARED=1
elif [ "$active_nvm" = "$SHARED_NVM_DIR" ] && [ -s "$SHARED_NVM_DIR/nvm.sh" ]; then
  # Same nvm installation. Probe read-only whether our writes would be no-ops.
  # subshell so sourcing nvm here cannot alter the caller's environment.
  if ( # shellcheck disable=SC1090,SC1091
    \. "$SHARED_NVM_DIR/nvm.sh" >/dev/null 2>&1
    nvm which "$PI_NODE_VERSION" >/dev/null 2>&1 || exit 1
    wanted_ver="$(nvm version "$PI_NODE_VERSION" 2>/dev/null)"
    def="$(nvm alias default 2>/dev/null | sed -n 's/.*-> \([^ ]*\).*/\1/p')"
    def_ver="$(nvm version "$def" 2>/dev/null)"
    [ "$def_ver" = "$wanted_ver" ] || exit 1
    wanted_bin="$(dirname "$(nvm which "$PI_NODE_VERSION")")"
    cur_target="$(readlink -f "$SHARED_NVM_DIR/current" 2>/dev/null || true)"
    [ "$cur_target" = "$(readlink -f "$wanted_bin")" ]
  ); then
    REUSE_SHARED=1
  fi
fi

if [ "$REUSE_SHARED" -eq 1 ]; then
  NVM_DIR="$SHARED_NVM_DIR"
else
  NVM_DIR="$PI_NVM_DIR_DEFAULT"
fi
export NVM_DIR

# The nvm installer requires the target directory to exist when NVM_DIR is
# set explicitly; create it first or a private install is refused.
mkdir -p "$NVM_DIR"

if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
fi

# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion" 2>/dev/null || true

# Install pi's node into nvm if this version is not already present. This runs
# regardless of whether some other node exists, which was the original bug.
PI_NODE_BIN=""
if command -v nvm >/dev/null 2>&1; then
  if ! nvm which "$PI_NODE_VERSION" >/dev/null 2>&1; then
    nvm install "$PI_NODE_VERSION"
  fi
  nvm use "$PI_NODE_VERSION" >/dev/null 2>&1 || true

  # In the isolated case, only set the default alias inside the private nvm so
  # the container's own default is never touched.
  if [ "$REUSE_SHARED" -eq 0 ]; then
    nvm alias default "$PI_NODE_VERSION" >/dev/null 2>&1 || true
  fi

  # Pin a stable symlink to the selected node's bin, mirroring the Dockerfile.
  # Fresh shells do not inherit `nvm use`, so this is what makes pi resolvable
  # regardless of the container's node path.
  PI_NODE_BIN="$(dirname "$(nvm which "$PI_NODE_VERSION")")"
  ln -sfn "$PI_NODE_BIN" "$NVM_DIR/current"
fi

if [ -z "$PI_NODE_BIN" ] || [ ! -x "$PI_NODE_BIN/node" ]; then
  # Fall back to whatever node the environment already provides rather than
  # writing a wrapper that cannot run.
  system_node="$(command -v node 2>/dev/null || true)"
  if [ -z "$system_node" ]; then
    echo "ERROR: no node available for pi (nvm install failed and no system node)" >&2
    exit 1
  fi
  PI_NODE_BIN="$(dirname "$system_node")"
  echo "WARNING: nvm did not provide node ${PI_NODE_VERSION}; using $PI_NODE_BIN/node" >&2
fi

mkdir -p "$BINDIR" "$HOME/.pi/agent/extensions"
# npm 11.20+ gates install scripts behind allowScripts. pi's current
# dependency tree needs these three allowed; if a future pi adds more, the
# install prints a warning naming them and this list can be extended.
npm install -g --allow-scripts=esbuild,@google/genai,protobufjs \
  @earendil-works/pi-coding-agent

# Expose pi through BINDIR without touching the container's own node. The
# wrapper prepends pi's node bin to PATH for that invocation only, so the
# npm shim's `#!/usr/bin/env node` shebang always resolves to the matching
# node even when the host PATH points at a different version. "pi" is the
# only name that must be global; the container keeps its own node/npm.
PI_BIN="$PI_NODE_BIN"
[ -x "$PI_BIN/pi" ] || {
  echo "ERROR: pi was not installed at $PI_BIN/pi; review the npm output above" >&2
  exit 1
}
cat > "$BINDIR/pi" <<EOF
#!/usr/bin/env bash
PATH="$PI_BIN:\$PATH" exec "$PI_BIN/pi" "\$@"
EOF
chmod +x "$BINDIR/pi"

# Only add pi's node to PATH if we did not have to isolate from a foreign node.
# In both cases BINDIR (which carries the pi wrapper) must be on PATH.
# CASE_PATH_LINE is written verbatim into the profile so $NVM_DIR/$BINDIR
# expand at login time, hence the single quotes and SC2016 disable.
# shellcheck disable=SC2016
if [ "$REUSE_SHARED" -eq 1 ]; then
  CASE_PATH_LINE='export PATH="$NVM_DIR/current:$BINDIR:$PATH"'
else
  CASE_PATH_LINE='export PATH="$BINDIR:$PATH"'
fi

# Make pi resolvable in the current shell for the remaining commands.
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) PATH="$BINDIR:$PATH" ;;
esac

cat > "$HOME/.pi/agent/extensions/global-guidelines.js" <<'EOF'
export default function addGuidelines(pi) {
  pi.on("before_agent_start", async (event) => {
    const datestr = new Date().toISOString().slice(0, 10);
    const customRule = "\n\n## Global Guidelines:\n" +
      "- Never use emojis, slang, or metaphors.\n" +
      "- Never claim code is verified unless you have actually run it.\n" +
      "- Always use up-to-date versions (e.g., python 3.10-3.14) when possible.\n" +
      "- If you are in a repo with a .pre-commit-config.yaml, pre-commit must be run and pass on all generated or altered code. You may not alter hooks or ignore rules without first getting approval.\n" +
      "- When modifying existing code, prefer small changes to major refactors unless otherwise instructed.\n" +
      "- When testing, if there is a tox configuration, used tox to run test suites rather than manually invoking them. Running a single test without tox for faster iteration is acceptable, but tox should be used for multiple tests.\n" +
      "- Never perform unrequested refactoring, cleanup, or structural changes. If it wasn't explicitly asked to be changed, leave it intact.\n" +
      "- The current date is " + datestr + ". Treat this as authoritative runtime context.\n" +
      "- Your training data may be outdated. Do not use the apparent absence of a model, package, library, API, or feature from your training data as evidence that it does not exist.\n" +
      "- If you share any links, they must be verified as active and not returning error codes.";
    return { systemPrompt: event.systemPrompt + customRule };
  });
}
EOF

cat > "$HOME/.pi/agent/settings.json" <<'EOF'
{
  "defaultModel": "qwen3.6:35b",
  "defaultProjectTrust": "always",
  "defaultProvider": "ollama",
  "defaultThinkingLevel": "minimal",
  "enableInstallTelemetry": false,
  "followUpMode": "all",
  "hideThinkingBlock": true,
  "httpIdleTimeoutMs": 0,
  "outputPad": 0,
  "quietStartup": true,
  "steeringMode": "all",
  "tuiMode": "regular"
}
EOF

cat > "$HOME/.pi/agent/local-providers.json" <<'EOF'
{
  "debug": false,
  "syncOnStartup": true,
  "addToScope": true,
  "providers": {
    "ollama": {
      "enabled": true,
      "baseUrl": "http://host.docker.internal:11434",
      "cleanupStale": true,
      "cacheTtlHours": 24
    }
  }
}
EOF

cat > "$HOME/.pi/agent/models.json" <<'EOF'
{
  "providers": {
    "ollama": {
      "baseUrl": "http://host.docker.internal:11434/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "compat": {
        "supportsDeveloperRole": false
      },
      "models": [
        { "id": "qwen3.6:35b" }
      ]
    },
    "local-llm": {
      "baseUrl": "https://router.huggingface.co/v1",
      "api": "openai-completions",
      "apiKey": "hf_xxx",
      "enabled": true,
      "models": []
    }
  }
}
EOF

cat > "$HOME/.pi/agent/compaction-continue.json" <<'EOF'
{
  "enabled": true,
  "appendSessionEntries": true,
  "log": true,
  "maxRecentEvents": 20
}
EOF

# pi install npm:@kylebrodeur/pi-model-discovery
pi install git:github.com/manthey/pi-model-discovery@dist
pi install npm:@richardgill/pi-up-history
pi install npm:@alexleekt/pi-bump
pi install npm:@badliveware/pi-compaction-continue
pi install npm:pi-loop-police

cat > "$BINDIR/pidev.sh" <<'EOF'
#!/usr/bin/env bash
pi --mode json --model "$1" "$2" "${@:3}" | jq -c 'select(.type != "message_update")'
EOF
chmod +x "$BINDIR/pidev.sh"
"$BINDIR/pidev.sh" x x --help 2>/dev/null >/dev/null

cat > "$BINDIR/set_ollama.sh" <<'EOF'
#!/usr/bin/env bash
# Set the ollama provider urls for pi. Only the ollama provider entries are
# modified; other providers (for example the HuggingFace router) keep their
# configured urls. The value is replaced wholesale, so this also repairs
# previously mangled urls, and re-running it is idempotent.
TARGET_URL="${1:-}"
if [ -z "$TARGET_URL" ]; then
  # Default to the ollama on the VM host. Under docker, host.docker.internal
  # is provided by the container runtime (--add-host). In a tart macOS VM
  # there is no docker; the host is the NAT gateway from the routing table.
  case "$(uname -s)" in
  Darwin)
    gateway="$(netstat -nr | awk '/^default/{print $2; exit}')"
    if [ -z "$gateway" ]; then
      echo "ERROR: could not determine the default gateway for the ollama host" >&2
      exit 1
    fi
    TARGET_URL="http://$gateway:11434"
    ;;
  *)
    TARGET_URL="http://host.docker.internal:11434"
    ;;
  esac
fi
# Normalize: add a scheme to bare host:port values, then strip any trailing
# slashes and /v1 suffix; the /v1 suffix is added below where it belongs.
case "$TARGET_URL" in
*://*) ;;
*) TARGET_URL="http://$TARGET_URL" ;;
esac
while :; do
  case "$TARGET_URL" in
  */) TARGET_URL="${TARGET_URL%/}" ;;
  */v1) TARGET_URL="${TARGET_URL%/v1}" ;;
  *) break ;;
  esac
done
cd "$HOME/.pi/agent" || exit 1
for file in local-providers.json models.json; do
  [ -f "$file" ] || { echo "ERROR: $file not found" >&2; exit 1; }
done
# local-providers.json uses the bare host url; models.json uses the /v1
# OpenAI-compatible endpoint.
jq --arg base "$TARGET_URL" '.providers.ollama.baseUrl = $base' \
  local-providers.json > local-providers.json.tmp &&
  mv local-providers.json.tmp local-providers.json
jq --arg base "$TARGET_URL" '.providers.ollama.baseUrl = ($base + "/v1")' \
  models.json > models.json.tmp && mv models.json.tmp models.json
echo "baseUrl values now:"
grep -h '"baseUrl"' local-providers.json models.json
EOF
chmod +x "$BINDIR/set_ollama.sh"

cat > "$BINDIR/set_hf.sh" <<'EOF'
#!/usr/bin/env bash
# set_hf.sh - Configure HuggingFace token and models for pi agent
# Usage: set_hf.sh [hf_token] [model1] [context1] [model2] [context2] ...
MODELS_JSON="$HOME/.pi/agent/models.json"
SETTINGS_JSON="$HOME/.pi/agent/settings.json"
DEFAULT_CONTEXT=262144
HF_TOKEN=""
declare -a MODELS=()
declare -A CONTEXTS=()
LAST_MODEL=""
for arg in "$@"; do
  if [[ "$arg" == hf_* ]]; then
    HF_TOKEN="$arg"
  elif [[ "$arg" =~ ^[0-9]+$ ]]; then
    if [[ -n "$LAST_MODEL" ]]; then
      CONTEXTS["$LAST_MODEL"]="$arg"
    fi
  else
    MODELS+=("$arg")
    LAST_MODEL="$arg"
  fi
done
if [[ $# -eq 0 ]]; then
  echo "Current HuggingFace token:"
  jq -r '.providers["local-llm"].apiKey // "not set"' "$MODELS_JSON"
  echo ""
  echo "Current HuggingFace models:"
  jq -r '.providers["local-llm"].models[].id // "none"' "$MODELS_JSON" 2>/dev/null
  exit 0
fi
if [[ -n "$HF_TOKEN" ]]; then
  jq --arg token "$HF_TOKEN" '.providers["local-llm"].apiKey = $token' "$MODELS_JSON" > "${MODELS_JSON}.tmp" && mv "${MODELS_JSON}.tmp" "$MODELS_JSON"
  echo "Token updated"
fi
for model in "${MODELS[@]}"; do
  ctx="${CONTEXTS[$model]:-$DEFAULT_CONTEXT}"
  jq --arg id "$model" --argjson ctx "$ctx" '.providers["local-llm"].models = [.providers["local-llm"].models[] | select(.id != $id)] + [{id: $id, contextWindow: $ctx}]' "$MODELS_JSON" > "${MODELS_JSON}.tmp" && mv "${MODELS_JSON}.tmp" "$MODELS_JSON"
  enabled_model="local-llm/$model"
  jq --arg m "$enabled_model" 'if (.enabledModels // []) | index($m) then . else .enabledModels = ((.enabledModels // []) + [$m]) end' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
  echo "Added model: $model (context: $ctx)"
done
if [[ -n "$LAST_MODEL" ]]; then
  jq --arg m "local-llm/$LAST_MODEL" '.defaultModel = $m' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
  echo "Default model set to: local-llm/$LAST_MODEL"
fi
EOF
chmod +x "$BINDIR/set_hf.sh"

PROFILE_BLOCK="$(cat <<EOF
export BINDIR="\${XDG_BIN_HOME:-\$HOME/.local/bin}"
export NVM_DIR="$NVM_DIR"
[ -s "\$NVM_DIR/nvm.sh" ] && \. "\$NVM_DIR/nvm.sh"
$CASE_PATH_LINE
EOF
)"
PROFILE="${XDG_CONFIG_HOME:-$HOME}/bashrc"
[ -f "$PROFILE" ] || PROFILE="$HOME/.bashrc"
[ -f "$PROFILE" ] || touch "$PROFILE"
grep -qF 'BIN_DIR' "$PROFILE" || printf '%s\n' "$PROFILE_BLOCK" >> "$PROFILE"
grep -qF 'PI_OFFLINE=1' "$PROFILE" || cat <<'EOF' >> "$PROFILE"
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
EOF
# zsh is the default shell on macOS; mirror the bash profile additions there.
ZPROFILE="${ZDOTDIR:-$HOME}/.zshrc"
[ -f "$ZPROFILE" ] || touch "$ZPROFILE"
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = zsh ]; then
  grep -qF 'BIN_DIR' "$ZPROFILE" || printf '%s\n' "$PROFILE_BLOCK" >> "$ZPROFILE"
  grep -qF 'PI_OFFLINE=1' "$ZPROFILE" || cat <<'EOF' >> "$ZPROFILE"
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
EOF
fi

command -v jq >/dev/null 2>&1 || curl -s https://webinstall.dev/jq | bash
[ -f "$HOME/.config/envman/PATH.env" ] && source "$HOME/.config/envman/PATH.env"

# Fail loudly if pi does not actually run; the wrapper resolves its own node.
"$BINDIR/pi" --help >/dev/null 2>&1 || {
  echo "ERROR: pi did not run successfully; review the npm output above" >&2
  exit 1
}

echo "Done."
