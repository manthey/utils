#!/usr/bin/env bash

# Standardize binary directory based on XDG spec or default fallback
export BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
NVM_DIR="${NVM_DIR:-$HOME/.nvm}"

# (a) Install npm via nvm if not already present
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
fi

[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" [ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion" 2>/dev/null || true

# Ensure node is available
if ! command -v node &>/dev/null; then
  nvm install 22
  nvm alias default 22
fi

# Install the main pi agent cli
npm install -g @earendil-works/pi-coding-agent > /dev/null 2>&1

mkdir -p "$BINDIR" "$HOME/.pi/agent/extensions"

echo "Configuring Pi..."

# (b) Write configuration files from sandbox definition
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
  "steeringMode": "all",
  "quietStartup": true
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

# Install all pip and npm extensions defined in the Dockerfile
pi install git:github.com/manthey/pi-model-discovery@dist > /dev/null 2>&1 || true
pi install npm:@richardgill/pi-up-history > /dev/null 2>&1 || true
pi install npm:@alexleekt/pi-bump > /dev/null 2>&1 || true
pi install npm:@badliveware/pi-compaction-continue > /dev/null 2>&1 || true
pi install npm:pi-loop-police > /dev/null 2>&1 || true

# global-guidelines.js extension
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
      "- Never perform unrequested refactoring, cleanup, or structural changes. If it wasn't explicitly asked to be changed, leave it intact.\n" +
      "- The current date is " + datestr + ". Treat this as authoritative runtime context.\n" +
      "- Your training data may be outdated. Do not use the apparent absence of a model, package, library, API, or feature from your training data as evidence that it does not exist.\n" +
      "- If you share any links, they must be verified as active and not returning error codes.";
    return { systemPrompt: event.systemPrompt + customRule };
  });
}
EOF

echo "Installing utility scripts..."

# Helper script to pipe raw JSON output
cat > "$BINDIR/pidev.sh" <<'EOF'
#!/usr/bin/env bash
pi --mode json --model "$1" "$2" "${@:3}" | jq -c 'select(.type != "message_update")'
EOF
chmod +x "$BINDIR/pidev.sh"

# Helper script to update HuggingFace token and local models (from Dockerfile)
cat > "$BINDIR/set_hf.sh" <<'SET_HF_EOF'
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
SET_HF_EOF
chmod +x "$BINDIR/set_hf.sh"

# Helper script to update Ollama URL (from Dockerfile)
cat > "$BINDIR/set_ollama.sh" <<'SET_OLLA_EOF'
#!/usr/bin/env bash
TARGET_URL="${1:-http://host.docker.internal:11434}"
sed -i 's|\("baseUrl": "\)https\?://[^/"]*|\1'"${TARGET_URL}"'|' "$HOME/.pi/agent/local-providers.json"
sed -i 's|\("baseUrl": "\)https\?://[^/"]*|\1'"${TARGET_URL}"'|' "$HOME/.pi/agent/models.json"
echo "${TARGET_URL}"
SET_OLLA_EOF
chmod +x "$BINDIR/set_ollama.sh"

# Persist environment variables in shell config (idempotent-ish)
PROFILE="${XDG_CONFIG_HOME:-$HOME}/bashrc" [ ! -f "$PROFILE" ] && PROFILE="$HOME/.bashrc"
grep -qF 'BIN_DIR' "$PROFILE" || cat <<'BM_EOF' >> "$PROFILE"
export BINDIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
export PATH="$BINDIR:$PATH"
BM_EOF

grep -qF 'PI_OFFLINE=1' "$PROFILE" || cat <<'PI_EOF' >> "$PROFILE"
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
export PYENV_ROOT="/.pyenv"
export CFLAGS="-std=gnu17 -march=native"
PI_EOF

echo "Done."
