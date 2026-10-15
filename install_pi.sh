#!/usr/bin/env bash

# Standardize binary directory based on XDG spec or default fallback
export BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"

# Node version pi requires. Keep this independent of whatever node the host
# container provides so pi can run even when no common version exists.
# On termux this is only a preference: the packaged node is built against
# bionic libc, and node builds that nvm downloads (glibc) cannot execute
# there, so the package manager's node is used whatever version it has.
PI_NODE_VERSION="${PI_NODE_VERSION:-22}"

# Detect the supported environments once up front. TERMUX_VERSION is set by
# the termux environment itself, with uname -o reporting Android as fallback.
# Git Bash / MSYS / Cygwin is the windows environment.
IS_TERMUX=0
if [ -n "${TERMUX_VERSION:-}" ] || [ "$(uname -o 2>/dev/null)" = "Android" ]; then
  IS_TERMUX=1
fi
IS_WINDOWS=0
case "$(uname -s)" in
MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
esac

# Provide the node binary that pi will run under, in PI_NODE_BIN, without
# disturbing whatever node management the environment already has.
#
# - termux: node comes from the package manager and nvm is not involved at
#   all, since the node binaries nvm downloads cannot run on bionic libc.
# - windows (Git Bash): nvm-sh is not supported there either; pi's node must
#   already be on PATH, which the agent sandbox payload guarantees with its
#   portable node.
# - everywhere else (docker, tart mac): use nvm. We must not mutate the host's
#   node setup: `nvm install`, `nvm alias default`, and repointing
#   `$NVM_DIR/current` are all write operations. So we reuse the host's nvm
#   ONLY when it is the same NVM_DIR, already has PI_NODE_VERSION installed,
#   and already has default and `current` pointing at that version (i.e. all
#   our writes would be no-ops). Otherwise we isolate into a private NVM_DIR
#   and leave the host alone. This covers host node supplied by nvm, by a
#   distro package, or by any other version manager.
PI_NODE_BIN=""
REUSE_SHARED=0
if [ "$IS_TERMUX" -eq 1 ]; then
  # Install node and jq through pkg if they are missing.
  if ! command -v node >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    pkg install -y nodejs-lts jq 2>/dev/null || {
      pkg update -y >/dev/null 2>&1 || true
      pkg install -y nodejs-lts jq
    }
  fi
  if ! command -v node >/dev/null 2>&1; then
    # nodejs-lts unavailable in this repo snapshot; try the current branch.
    pkg install -y nodejs 2>/dev/null || true
  fi
  termux_node="$(command -v node 2>/dev/null || true)"
  if [ -n "$termux_node" ]; then
    PI_NODE_BIN="$(dirname "$termux_node")"
    termux_node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
    if [ -n "$termux_node_major" ] && [ "$termux_node_major" != "$PI_NODE_VERSION" ]; then
      echo "WARNING: termux provides node ${termux_node_major}; requested node ${PI_NODE_VERSION} will not be installed there" >&2
    fi
  fi
elif [ "$IS_WINDOWS" -eq 1 ]; then
  # Windows: pi's node comes from PATH (portable node in the sandbox payload).
  PI_NODE_BIN="$(dirname "$(command -v node 2>/dev/null || true)")"
  if [ -z "$PI_NODE_BIN" ] ||
    { [ ! -x "$PI_NODE_BIN/node" ] && [ ! -x "$PI_NODE_BIN/node.exe" ]; }; then
    echo "ERROR: no node available for pi (on Windows the payload must provide node on PATH)" >&2
    exit 1
  fi
  # The portable node directory is mapped read-only into the sandbox, so
  # global npm installs need a writable prefix.
  export NPM_CONFIG_PREFIX="$HOME/.npm-global"
  mkdir -p "$NPM_CONFIG_PREFIX"
else
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

  if [ -z "$active_node" ]; then
    # No node at all: safe to use the shared nvm as the host has none.
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

  # Install pi's node into nvm if this version is not already present. This
  # runs regardless of whether some other node exists, which was the original
  # bug.
  if command -v nvm >/dev/null 2>&1; then
    if ! nvm which "$PI_NODE_VERSION" >/dev/null 2>&1; then
      nvm install "$PI_NODE_VERSION"
    fi
    nvm use "$PI_NODE_VERSION" >/dev/null 2>&1 || true

    # In the isolated case, only set the default alias inside the private nvm
    # so the host's own default is never touched.
    if [ "$REUSE_SHARED" -eq 0 ]; then
      nvm alias default "$PI_NODE_VERSION" >/dev/null 2>&1 || true
    fi

    # Pin a stable symlink to the selected node's bin, mirroring the
    # Dockerfile. Fresh shells do not inherit `nvm use`, so this is what makes
    # pi resolvable regardless of the host's node path.
    PI_NODE_BIN="$(dirname "$(nvm which "$PI_NODE_VERSION")")"
    ln -sfn "$PI_NODE_BIN" "$NVM_DIR/current"
  fi
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
  echo "WARNING: requested node ${PI_NODE_VERSION} was not provided; using $PI_NODE_BIN/node" >&2
fi

mkdir -p "$BINDIR" "$HOME/.pi/agent/extensions"
# npm 11.20+ gates install scripts behind allowScripts. pi's current
# dependency tree needs these three allowed; if a future pi adds more, the
# install prints a warning naming them and this list can be extended.
npm install -g --allow-scripts=esbuild,@google/genai,protobufjs \
  @earendil-works/pi-coding-agent

# Expose pi without touching the environment's own node. On the nvm paths the
# wrapper prepends pi's node bin to PATH for that invocation only, so the npm
# shim's `#!/usr/bin/env node` shebang always resolves to the matching node
# even when the host PATH points at a different version. "pi" is the only
# name that must be global; the host keeps its own node/npm.
PI_BIN="$PI_NODE_BIN"
if [ "$IS_WINDOWS" -eq 1 ]; then
  # Windows: npm's own shims (pi.cmd / pi.ps1 / pi) live in
  # NPM_CONFIG_PREFIX, which the sandbox provisioner puts on PATH. No wrapper
  # is needed; the wrapper below would also point at the read-only node
  # directory instead of the writable prefix where pi was installed.
  PATH="$NPM_CONFIG_PREFIX:$PATH"
  [ -x "$NPM_CONFIG_PREFIX/pi" ] || [ -x "$NPM_CONFIG_PREFIX/pi.cmd" ] || {
    echo "ERROR: pi was not installed at $NPM_CONFIG_PREFIX/pi; review the npm output above" >&2
    exit 1
  }
else
  [ -x "$PI_BIN/pi" ] || {
    echo "ERROR: pi was not installed at $PI_BIN/pi; review the npm output above" >&2
    exit 1
  }
  cat > "$BINDIR/pi" <<EOF
#!/usr/bin/env bash
PATH="$PI_BIN:\$PATH" exec "$PI_BIN/pi" "\$@"
EOF
  chmod +x "$BINDIR/pi"
fi

# Only add pi's node to PATH if we did not have to isolate from a foreign node.
# In both cases BINDIR (which carries the pi wrapper) must be on PATH.
# CASE_PATH_LINE is written verbatim into the profile so $NVM_DIR/$BINDIR
# expand at login time, hence the single quotes and SC2016 disable.
# shellcheck disable=SC2016
if [ "$IS_TERMUX" -eq 1 ] || [ "$IS_WINDOWS" -eq 1 ]; then
  CASE_PATH_LINE='export PATH="$BINDIR:$PATH"'
elif [ "$REUSE_SHARED" -eq 1 ]; then
  # Append $NVM_DIR/current rather than prepending it: ahead of nvm's own bin
  # dirs the symlink shadows every `nvm use`. The pi wrapper in BINDIR pins
  # pi's node regardless, so the symlink only serves non-interactive shells.
  CASE_PATH_LINE='export PATH="$BINDIR:$PATH:$NVM_DIR/current"'
else
  CASE_PATH_LINE='export PATH="$BINDIR:$PATH"'
fi

# Make pi resolvable in the current shell for the remaining commands. The
# parent shell's PATH is captured first: a child process cannot modify it, so
# the closing message uses it to tell the user when their shell still needs
# the export before pi resolves.
ORIGINAL_PATH="$PATH"
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
  # Default to the ollama for this environment. On termux there is no
  # container host; ollama typically runs on the device itself, so localhost.
  # Under docker, host.docker.internal is provided by the container runtime
  # (--add-host). In a tart macOS VM there is no docker; the host is the NAT
  # gateway from the routing table. On windows (Git Bash in the agent
  # sandbox) the sandbox's default route gateway is the host; route print is
  # locale independent.
  if [ -n "${TERMUX_VERSION:-}" ] || [ "$(uname -o 2>/dev/null)" = "Android" ]; then
    TARGET_URL="http://localhost:11434"
  else
    case "$(uname -s)" in
    Darwin)
      gateway="$(netstat -nr | awk '/^default/{print $2; exit}')"
      if [ -z "$gateway" ]; then
        echo "ERROR: could not determine the default gateway for the ollama host" >&2
        exit 1
      fi
      TARGET_URL="http://$gateway:11434"
      ;;
    MINGW*|MSYS*|CYGWIN*)
      gateway="$(route print -4 | awk '/ 0\.0\.0\.0 +0\.0\.0\.0 /{print $3; exit}')"
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
#
# Model ids may carry a routing suffix:
#   :<provider>   explicit provider (e.g. :novita)
#   :cheapest     HF routes to the cheapest live provider
#   :fastest      HF routes to the fastest live provider
#   (no suffix)   HF defaults to :fastest
#   :ourpick      resolved locally by this script: among providers that
#                 publish a price (input + output), keep the lowest total
#                 price, then the largest context_length, then the highest
#                 throughput; the id is rewritten to :<provider> in
#                 models.json.
#
# When no context size is given on the command line, the script fetches
# context_length for the resolved provider from the HF router's /v1/models.
MODELS_JSON="$HOME/.pi/agent/models.json"
SETTINGS_JSON="$HOME/.pi/agent/settings.json"
DEFAULT_CONTEXT=262144
HF_ROUTER="https://router.huggingface.co/v1"
HF_TOKEN=""
declare -a MODELS=()
declare -A CONTEXTS=()
declare -A FINAL_IDS=()
LAST_MODEL=""

# Resolve a model id (with optional suffix) against the HF router.
# Prints "<provider>\t<context_length>" on success, nothing on failure.
fetch_context() {
  local model="$1" token="$2" base sfx response
  base="$model"
  sfx=""
  if [[ "$model" == *:* ]]; then
    base="${model%%:*}"
    sfx="${model#*:}"
  fi
  token="${token:-$(jq -r '.providers["local-llm"].apiKey // empty' "$MODELS_JSON")}"
  [[ -n "$token" ]] || return 1
  response="$(curl -sf --max-time 15 "$HF_ROUTER/models" -H "Authorization: Bearer $token")" || return 1
  jq -r --arg id "$base" --arg sfx "$sfx" '
    def live: [.providers[]? | select(.status == "live" and .context_length != null)];
    def priced: map(select(.pricing.input != null and .pricing.output != null));
    def resolve:
      if $sfx == "ourpick" then
        priced
        | (map(.pricing.input + .pricing.output) | min) as $p
        | map(select(.pricing.input + .pricing.output == $p))
        | (map(.context_length) | max) as $c
        | map(select(.context_length == $c))
        | max_by(.throughput // 0)
      elif $sfx == "cheapest" then
        priced
        | (map(.pricing.input) | min) as $p
        | map(select(.pricing.input == $p))
        | (map(.context_length) | max) as $c
        | map(select(.context_length == $c))
        | max_by(.throughput // 0)
      elif $sfx == "" or $sfx == "fastest" then
        (map(.throughput // 0) | max) as $t
        | map(select((.throughput // 0) == $t))
        | max_by(.context_length // 0)
      else
        map(select(.provider == $sfx))
        | if length == 0 then empty else .[0] end
      end;
    (.data[] | select(.id == $id or (.id | endswith("/" + $id))))
    | live
    | if length == 0 then empty else resolve end
    | if . == null then empty else "\(.provider)\t\(.context_length)" end
  ' <<<"$response"
}

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
  base="$model"
  sfx=""
  if [[ "$model" == *:* ]]; then
    base="${model%%:*}"
    sfx="${model#*:}"
  fi
  final_id="$model"
  ctx_spec="${CONTEXTS[$model]:-}"
  ctx=""
  resolved_ok=0
  if resolved="$(fetch_context "$model" "$HF_TOKEN")" && [[ -n "$resolved" ]]; then
    IFS=$'\t' read -r provider ctx <<<"$resolved"
    resolved_ok=1
    if [[ "$sfx" == "ourpick" ]]; then
      final_id="$base:$provider"
      echo "Resolved :ourpick for $base -> :$provider (context: $ctx)"
    fi
  fi
  if [[ "$sfx" == "ourpick" && "$resolved_ok" -eq 0 ]]; then
    # :ourpick is not an HF routing suffix; without the router listing it
    # cannot be resolved, so degrade to :cheapest which HF understands.
    final_id="$base:cheapest"
  fi
  if [[ -n "$ctx_spec" ]]; then
    if [[ "$resolved_ok" -eq 1 && "$ctx" -lt "$ctx_spec" ]]; then
      echo "Note: fetched context $ctx for $final_id is below the specified $ctx_spec; using $ctx"
    else
      ctx="$ctx_spec"
    fi
  elif [[ "$resolved_ok" -eq 0 ]]; then
    # Offline or unresolvable: do not clobber an existing contextWindow; only
    # a brand-new entry needs a fallback value.
    existing="$(jq -r --arg id "$final_id" '.providers["local-llm"].models[]? | select(.id == $id) | .contextWindow // empty' "$MODELS_JSON")"
    if [[ -n "$existing" ]]; then
      ctx="$existing"
      echo "Warning: could not resolve $model against the HF router; keeping existing context $ctx for $final_id" >&2
    else
      ctx="$DEFAULT_CONTEXT"
      echo "Warning: could not resolve $model against the HF router; using default context $ctx" >&2
    fi
  fi
  jq --arg id "$final_id" --argjson ctx "$ctx" '.providers["local-llm"].models = [.providers["local-llm"].models[] | select(.id != $id)] + [{id: $id, contextWindow: $ctx}]' "$MODELS_JSON" > "${MODELS_JSON}.tmp" && mv "${MODELS_JSON}.tmp" "$MODELS_JSON"
  enabled_model="local-llm/$final_id"
  jq --arg m "$enabled_model" 'if (.enabledModels // []) | index($m) then . else .enabledModels = ((.enabledModels // []) + [$m]) end' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
  if [[ "$final_id" != "$model" ]]; then
    # The un-resolved placeholder (e.g. :ourpick) should not linger in settings.
    jq --arg m "local-llm/$model" '.enabledModels = [(.enabledModels // [])[] | select(. != $m)]' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
  fi
  FINAL_IDS["$model"]="$final_id"
  echo "Added model: $final_id (context: $ctx)"
done
if [[ -n "$LAST_MODEL" ]]; then
  jq --arg m "local-llm/${FINAL_IDS[$LAST_MODEL]:-$LAST_MODEL}" '.defaultModel = $m' "$SETTINGS_JSON" > "${SETTINGS_JSON}.tmp" && mv "${SETTINGS_JSON}.tmp" "$SETTINGS_JSON"
  echo "Default model set to: local-llm/${FINAL_IDS[$LAST_MODEL]:-$LAST_MODEL}"
fi
EOF
chmod +x "$BINDIR/set_hf.sh"

if [ "$IS_TERMUX" -eq 1 ]; then
  # Termux's bash sources $PREFIX/etc/bash.bashrc at login, not ~/.bashrc,
  # and there is no nvm to wire up; only BINDIR needs exporting.
  PROFILE_BLOCK="$(cat <<EOF
export BINDIR="\${XDG_BIN_HOME:-\$HOME/.local/bin}"
$CASE_PATH_LINE
EOF
)"
  PROFILE="${PREFIX:-/data/data/com.termux/files/usr}/etc/bash.bashrc"
else
  PROFILE_BLOCK="$(cat <<EOF
export BINDIR="\${XDG_BIN_HOME:-\$HOME/.local/bin}"
export NVM_DIR="$NVM_DIR"
[ -s "\$NVM_DIR/nvm.sh" ] && \. "\$NVM_DIR/nvm.sh"
$CASE_PATH_LINE
EOF
)"
  PROFILE="${XDG_CONFIG_HOME:-$HOME}/bashrc"
  [ -f "$PROFILE" ] || PROFILE="$HOME/.bashrc"
fi
[ -f "$PROFILE" ] || touch "$PROFILE"
grep -qF 'export BINDIR=' "$PROFILE" || printf '%s\n' "$PROFILE_BLOCK" >> "$PROFILE"
grep -qF 'PI_OFFLINE=1' "$PROFILE" || cat <<'EOF' >> "$PROFILE"
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
EOF
# zsh is the default shell on macOS; mirror the bash profile additions there.
ZPROFILE="${ZDOTDIR:-$HOME}/.zshrc"
[ -f "$ZPROFILE" ] || touch "$ZPROFILE"
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = zsh ]; then
  grep -qF 'export BINDIR=' "$ZPROFILE" || printf '%s\n' "$PROFILE_BLOCK" >> "$ZPROFILE"
  grep -qF 'PI_OFFLINE=1' "$ZPROFILE" || cat <<'EOF' >> "$ZPROFILE"
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
EOF
fi

if [ "$IS_TERMUX" -eq 0 ]; then
  # jq came from pkg on termux; elsewhere use the webinstall script, which
  # also wires up its envman PATH entries.
  command -v jq >/dev/null 2>&1 || curl -s https://webinstall.dev/jq | bash
  [ -f "$HOME/.config/envman/PATH.env" ] && source "$HOME/.config/envman/PATH.env"
fi

# Fail loudly if pi does not actually run; the wrapper resolves its own node.
if [ "$IS_WINDOWS" -eq 1 ]; then
  pi --help >/dev/null 2>&1 || {
    echo "ERROR: pi did not run successfully; review the npm output above" >&2
    exit 1
  }
else
  "$BINDIR/pi" --help >/dev/null 2>&1 || {
    echo "ERROR: pi did not run successfully; review the npm output above" >&2
    exit 1
  }
fi

# The config files were written with the docker default ollama url above.
# Apply the platform-appropriate default instead: localhost on termux (ollama
# runs on the device), the host gateway on a tart mac or windows sandbox, and
# host.docker.internal under docker. set_ollama.sh is idempotent; jq is
# present on every path that reaches here (pkg on termux, webinstall
# elsewhere).
if command -v jq >/dev/null 2>&1; then
  "$BINDIR/set_ollama.sh" >/dev/null
else
  echo "WARNING: jq unavailable; the ollama url was not localized; run set_ollama.sh" >&2
fi

# A script cannot modify its parent shell's PATH. When the invoking shell did
# not already carry BINDIR, point the user at the one-line export instead of
# leaving them to discover the helpers only after opening a new terminal.
PATH_WAS_SET=0
case ":$ORIGINAL_PATH:" in *":$BINDIR:"*) PATH_WAS_SET=1 ;; esac
if [ "$PATH_WAS_SET" -eq 1 ]; then
  echo "Done."
else
  echo "Done. To use pi and its helper scripts in this shell, run:"
  echo "  export PATH=\"$BINDIR:\$PATH\""
  echo "(new shells pick this up from the profile automatically)."
fi
