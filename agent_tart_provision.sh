#!/usr/bin/env bash
# agent_tart_provision.sh - runs INSIDE the tart macOS VM (as the admin user)
# to provision the base VM. This is the equivalent of agent.Dockerfile,
# minus mini-swe-agent (intentionally omitted) and minus components that only
# make sense in a Linux docker image (docker-cli, fuse, xvfb/mesa, rdfind
# shrinking). pi itself is installed by install_pi.sh, which is shared with
# the docker flow. See agent_tart.md.
#
# Optional environment variables:
#   PYTHONS                  pyenv versions to build (default 3.10-3.15)
#   MACOSX_DEPLOYMENT_TARGET lowest macOS for built wheels (default 11.0,
#                            the Apple Silicon floor; sets the wheel platform
#                            tag, e.g. macosx_11_0_arm64)
#
# Re-running this script is safe; completed steps are skipped.

set -euo pipefail

PYTHONS="${PYTHONS:-3.10 3.11 3.12 3.13 3.14 3.15}"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
export CFLAGS="${CFLAGS:--std=gnu17 -march=native}"
# Do not use -march=native for release wheels; it embeds host CPU features.
# It matches the docker image behavior for in-sandbox development builds.

step() {
    echo "=== $*"
}

# --- sudo

step "sudo access"
if ! sudo -n true 2>/dev/null; then
    printf '%s\n' "${TART_PASS:-admin}" | sudo -S true 2>/dev/null ||
        echo "WARNING: sudo needs a password; enter it at the prompt" >&2
fi
SUDOERS_FILE="/etc/sudoers.d/agent-nopasswd"
if [ ! -f "$SUDOERS_FILE" ]; then
    echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee "$SUDOERS_FILE" >/dev/null
    sudo chmod 440 "$SUDOERS_FILE"
fi

step "disk space"
avail_gb="$(df -g / | awk 'NR == 2 {print $4}')"
if [ "${avail_gb:-0}" -lt 40 ]; then
    cat >&2 <<'EOW'
WARNING: less than 40 GB free on the root volume. Provisioning needs roughly
40-60 GB for brew packages, pythons, and browsers. tart does NOT grow the
APFS container automatically after `tart set --disk-size`. To grow it, boot
the VM with `tart run --recovery <vm>`, open Terminal in recovery, then:
  diskutil list physical          # identify the APFS container (e.g. disk0s2)
  diskutil eraseVolume free free disk0s3   # remove the recovery partition
  yes | diskutil repairDisk disk0
  diskutil apfs resizeContainer disk0s2 0
then reboot normally. Continuing anyway in 60 seconds...
EOW
    sleep 60
fi

# --- Xcode Command Line Tools (compilers; required for pyenv and brew)

if ! xcode-select -p >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
    step "installing Xcode Command Line Tools (several minutes)"
    touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
    label="$(softwareupdate -l 2>/dev/null |
        sed -n 's/^[^L]*Label: "\(.*\)"/\1/p' |
        grep 'Command Line Tools' | head -1)"
    if [ -z "$label" ]; then
        rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
        echo "ERROR: no Command Line Tools label found via softwareupdate" >&2
        exit 1
    fi
    sudo softwareupdate --install "$label" --agree-license
    rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
    sudo xcode-select --switch /Library/Developer/CommandLineTools 2>/dev/null || true
fi

# --- Homebrew

step "Homebrew"
if ! command -v brew >/dev/null 2>&1 &&
    [ ! -x /opt/homebrew/bin/brew ] && [ ! -x /usr/local/bin/brew ]; then
    NONINTERACTIVE=1 /bin/bash -c "$(
        curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh
    )"
fi
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
command -v brew >/dev/null 2>&1 || {
    echo "ERROR: Homebrew installation failed" >&2
    exit 1
}

# --- brew packages (parallel to the agent.Dockerfile apt packages)

step "brew packages"
brew tap mongodb/brew 2>/dev/null || true
# Recent Homebrew refuses formulas from third-party taps unless trusted.
brew trust mongodb/brew 2>/dev/null || true
# pyenv build deps: readline sqlite3 xz zlib tcl-tk openssl
# wheel-building C libraries: libtiff openjpeg little-cms2 libffi
# services for testing: redis memcached rabbitmq (brings openjdk)
# mongodb-community
brew install \
    bash coreutils jq wget curl git \
    cmake ninja pkg-config ccache \
    readline sqlite3 xz zlib tcl-tk openssl@3 \
    libtiff openjpeg little-cms2 libffi \
    autoconf automake libtool \
    ripgrep fd vim tmux less pandoc imagemagick libmagic optipng xmlstarlet \
    shellcheck hadolint universal-ctags \
    redis memcached rabbitmq mongodb-community

# --- pyenv pythons

step "pyenv pythons: $PYTHONS"
export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
export PATH="$PYENV_ROOT/bin:$PATH"
if [ ! -x "$PYENV_ROOT/bin/pyenv" ]; then
    curl -L https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash
fi
export PATH="$PYENV_ROOT/shims:$PATH"
eval "$(pyenv init - 2>/dev/null)" || true

for version in $PYTHONS; do
    (
        if ! pyenv install -s "$version" 2>/dev/null; then
            echo "WARNING: pyenv install $version failed; continuing" >&2
        fi
    ) &
done
wait

for version in $PYTHONS; do
    if pyenv versions --bare | grep -qx "$version"; then
        PYENV_VERSION="$version" python -m pip install --quiet --upgrade pip setuptools wheel
        PYENV_VERSION="$version" python -m pip install --quiet tox coverage build delocate
    fi
done
# shellcheck disable=SC2046
pyenv global $(pyenv versions --bare) || true
pyenv rehash

# --- uv, the shared venv, and pi

step "uv"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv venv --clear --seed --no-project --python 3.13 "$HOME/.env" ||
    uv venv --clear --seed --no-project "$HOME/.env"

step "pi (via install_pi.sh)"
bash "$HOME/.agent_tart_provision/install_pi.sh"
export PATH="$HOME/.local/bin:$PATH"

# --- node 22 + playwright browsers

if [ -s "$HOME/.nvm-pi/nvm.sh" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.nvm-pi/nvm.sh"
    nvm use 22 >/dev/null 2>&1 || true
    step "playwright browsers"
    npx playwright install || true
fi

# --- services (macOS equivalent of the image's start_services.sh)

step "start_services.sh"
BREW_PREFIX="$(brew --prefix)"
mkdir -p "$HOME/.local/bin"
cat >"$HOME/.local/bin/start_services.sh" <<EOF
#!/usr/bin/env bash
# Start the test services the docker image provides (redis, mongo,
# rabbitmq, memcached).
BREW="\${BREW:-$BREW_PREFIX}"
pkill -x redis-server 2>/dev/null || true
pkill -x mongod 2>/dev/null || true
pkill -f rabbitmq-server 2>/dev/null || true
pkill -x memcached 2>/dev/null || true
sleep 1
"\$BREW/opt/redis/bin/redis-server" --bind 0.0.0.0 --daemonize yes
rm -rf /tmp/db && mkdir -p /tmp/db
"\$BREW/opt/mongodb-community/bin/mongod" --noauth --bind_ip_all \
    --dbpath=/tmp/db --fork --logpath /tmp/mongo.log
"\$BREW/opt/rabbitmq/sbin/rabbitmq-server" -detached
"\$BREW/opt/memcached/bin/memcached" -d
echo "services started"
EOF
chmod +x "$HOME/.local/bin/start_services.sh"

# --- shell environment

step "shell profiles"
for profile in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [ -f "$profile" ] || touch "$profile"
    grep -qF 'PYENV_ROOT' "$profile" || cat >>"$profile" <<EOF
export PYENV_ROOT="\$HOME/.pyenv"
export PATH="\$PYENV_ROOT/bin:\$PYENV_ROOT/shims:\$HOME/.local/bin:\$PATH"
export CFLAGS="$CFLAGS"
export MACOSX_DEPLOYMENT_TARGET="$MACOSX_DEPLOYMENT_TARGET"
export PI_OFFLINE=1
export PI_SKIP_VERSION_CHECK=1
eval "\$(pyenv init -)" 2>/dev/null || true
EOF
done

# --- git identity

git config --global user.name "VM"
git config --global user.email "container@example.com"

# --- cleanup

step "cleanup"
brew cleanup --prune=all >/dev/null 2>&1 || true
rm -rf "$HOME/Library/Caches/Homebrew" /tmp/* 2>/dev/null || true

echo "provisioning complete"
