#!/usr/bin/env bash
# agent_termux.sh - nested-termux sandbox equivalent of agent_docker.py.
#
# agent_docker.py builds agentic-programming sandboxes (running pi) from the
# agent.Dockerfile docker image, and agent_tart.sh does the same on an Apple
# Silicon Mac with tart VMs. This script does it entirely inside termux on an
# Android device, which makes it possible to develop and test for termux in
# the same disposable way:
#
# each sandbox is a nested termux: a termux bootstrap rootfs extracted under
# ~/.agent_termux/<name>/root (at the standard termux paths inside it) and
# run through proot -r, so the guest's / is the sandbox rootfs and the only
# host paths shared in are /proc, /dev, /sys, and the read-only OS
# partitions termux binaries need (/system, /apex, /linkerconfig). The
# guest is real bionic termux (its own $PREFIX, pkg, termux-packaged node)
# and cannot reach any other host path - writes land in the sandbox or
# fail, and stop wipes the whole sandbox directory. Commands and semantics
# match agent_docker.py (sandboxes are ephemeral: stop destroys the
# sandbox, and start always rebuilds one from the base) plus a cp command:
#
#   provision  download the bootstrap, extract the base, install pi in it
#   create     clone the base, copy the project in, shell in
#   start      like create, but does not shell in
#   stop       destroy the sandbox
#   exec       shell into a running sandbox
#   update     apply --ollama to a running sandbox
#   list       list known sandboxes
#   cp         copy files to/from the sandbox; one endpoint prefixed in:
#
# cp usage: agent_termux.sh cp [--name NAME] in:<guest path> <local path>
#           agent_termux.sh cp [--name NAME] <local path> in:<guest path>
#           (like docker cp; directories are copied recursively)
#
# A running sandbox is a proot-wrapped sshd on a localhost-only port, so
# exec and cp work over ssh like the other agents. The guest shares the
# device's network namespace, so its default ollama url (localhost:11434)
# reaches the same ollama as the host termux.
#
# The guest is provisioned by install_pi.sh (shared with the docker and tart
# flows), which runs its termux branch inside the sandbox.
#
# Host requirements (termux packages): proot, unzip, openssh, jq, git; the
# bootstrap zip is cached under ~/.agent_termux. Mounts are read-write
# (proot has no read-only enforcement); without an explicit --mount of
# shared storage the guest has no sdcard access.

set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="${TERMUX_AGENT_DIR:-$HOME/.agent_termux}"
LOG_DIR="$AGENT_DIR/logs"
SSH_DIR="$AGENT_DIR/ssh"

# The guest prefix is fixed by the bootstrap binaries, which hardcode
# /data/data/com.termux/files/usr; the sandbox rootfs mirrors that layout
# under its root/ directory so proot -r can present it as the guest's /.
guest_root() {
    printf '%s/%s/root' "$AGENT_DIR" "$1"
}

GUEST_PREFIX="/data/data/com.termux/files/usr"
GUEST_HOME="/data/data/com.termux/files/home"

BASE_NAME="${TERMUX_AGENT_BASE:-base}"
BOOTSTRAP_URL="${TERMUX_BOOTSTRAP_URL:-}"
SSH_PORT_BASE="${TERMUX_SSHPORT:-2222}"
# Provisioning and each clone need substantial space (base and sandbox are
# each hundreds of MB); refuse to start when the home filesystem cannot hold
# them instead of filling the disk mid-install.
MIN_FREE_KB="${TERMUX_MIN_FREE_KB:-2097152}"

BASE_IMAGE=""
NAME=""
NUM=""
SRC=""
SSH_PUB=""
SSH_KEY=""
COMMAND=""
LOCAL=0
OLLAMA=""
CONFIG_FILE="$SELF_DIR/agent_termux.yaml"
MOUNTS=()
SKIPS=()
POSITIONALS=()

die() {
    echo "agent_termux: $*" >&2
    exit 1
}

info() {
    echo "agent_termux: $*" >&2
}

usage() {
    # Print the header comment block as the help text; keep the range in sync
    # with the header above.
    sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

safe_name() {
    printf '%s' "$1" | sed 's/[<>:"/\\|?*[:cntrl:]]/_/g; s/[. ]*$//; s/^$/termux/'
}

parse_args() {
    MOUNTS=()
    SKIPS=()
    while [ $# -gt 0 ]; do
        case "$1" in
        provision | create | start | stop | exec | update | list | cp)
            COMMAND="$1"
            ;;
        --name)
            NAME="$2"
            shift
            ;;
        --num)
            NUM="$2"
            shift
            ;;
        --src)
            SRC="$2"
            shift
            ;;
        --base)
            BASE_NAME="$2"
            shift
            ;;
        --image)
            BASE_IMAGE="$2"
            shift
            ;;
        --mount)
            MOUNTS+=("$2")
            shift
            ;;
        --skip)
            SKIPS+=("$2")
            shift
            ;;
        --local)
            LOCAL=1
            ;;
        --no-local)
            LOCAL=0
            ;;
        --ollama | --url)
            OLLAMA="$2"
            shift
            ;;
        --sshkey)
            SSH_PUB="$2"
            shift
            ;;
        --config)
            CONFIG_FILE="$2"
            shift
            ;;
        -v)
            set -x
            ;;
        *)
            POSITIONALS+=("$1")
            ;;
        esac
        shift
    done
}

# Per-working-directory defaults, with the same semantics as add_config in
# agent_docker.py and apply_config in agent_tart.sh: arguments merge as
# "all" entries, then per-directory entries, then command-line arguments
# (later scalars win; --mount/--skip accumulate from every source).
apply_config() {
    [ -f "$CONFIG_FILE" ] || return 0
    command -v python3 >/dev/null 2>&1 || {
        info "python3 unavailable, ignoring config file $CONFIG_FILE"
        return 0
    }
    # Python emits the merged arguments shell-quoted so that values containing
    # spaces survive; the eval expansion splits them back into single words.
    local merged
    merged="$(python3 -c '
import shlex, sys
try:
    import yaml
except Exception:
    sys.exit(0)
config, key = sys.argv[1], sys.argv[2]
try:
    data = yaml.safe_load(open(config)) or {}
except Exception:
    sys.exit(0)
args = []
if "all" in data:
    args += list(data["all"])
if key in data:
    args += list(data[key])
print(" ".join(shlex.quote(str(a)) for a in args))
' "$CONFIG_FILE" "$PWD" 2>/dev/null)" || merged=""
    [ -n "$merged" ] || return 0
    local quoted_orig=""
    local a
    for a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
        quoted_orig="$quoted_orig $(printf '%q' "$a")"
    done
    # shellcheck disable=SC2086
    eval "parse_args $merged$quoted_orig"
}

ORIG_ARGS=("$@")
parse_args ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
[ -n "$COMMAND" ] || usage
apply_config

if [ -n "$SRC" ]; then
    cd "$SRC" || die "cannot cd to $SRC"
fi

for tool in proot unzip jq ssh scp ssh-keygen; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found; install it with: pkg install $tool"
done

mkdir -p "$LOG_DIR" "$SSH_DIR"
KNOWN_HOSTS="$SSH_DIR/known_hosts"

CURRENT_DIR="$(basename "$PWD")"
if [ -z "$NAME" ]; then
    NAME="agent_$(safe_name "$CURRENT_DIR")"
fi
VM_NAME="$NAME"
[ -n "$NUM" ] && VM_NAME="${NAME}_${NUM}"

vm_exists() {
    [ -d "$(guest_root "$1")$GUEST_PREFIX" ]
}

# A sandbox is running when its recorded proot pid is alive and that
# process's command line still references this sandbox directory.
vm_running() {
    local pidfile="$AGENT_DIR/$1/.sshd.pid" pid
    [ -r "$pidfile" ] || return 1
    read -r pid < "$pidfile"
    [ -n "$pid" ] || return 1
    [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "proot.*$1"
}

vm_port() {
    local portfile="$AGENT_DIR/$1/.sshd.port"
    [ -r "$portfile" ] && cat "$portfile"
}

# First free localhost port at or after $1. A successful /dev/tcp connect
# means something is already listening there.
free_port() {
    local port="$1"
    while :; do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            exec 3>&- 3<&- 2>/dev/null || true
            port=$((port + 1))
            continue
        fi
        printf '%s' "$port"
        return
    done
}

# Refuse to fill the disk: fail before the big allocations when the home
# filesystem has less than $MIN_FREE_KB available.
check_free_space() {
    local what="$1" avail
    [ "$MIN_FREE_KB" -gt 0 ] || return 0
    avail="$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
    [ -n "$avail" ] || return 0
    if [ "$avail" -lt "$MIN_FREE_KB" ]; then
        die "only ${avail}KB free on $HOME but $what needs about ${MIN_FREE_KB}KB; free space or lower TERMUX_MIN_FREE_KB"
    fi
}

ensure_ssh_key() {
    if [ -n "$SSH_PUB" ] && [ -f "$SSH_PUB" ]; then
        SSH_KEY="${SSH_PUB%.pub}"
        return 0
    fi
    if [ -n "$SSH_KEY" ]; then
        [ -f "$SSH_KEY.pub" ] || die "ssh key $SSH_KEY.pub not found"
        return 0
    fi
    for candidate in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_ecdsa.pub" "$HOME/.ssh/id_rsa.pub"; do
        if [ -f "$candidate" ]; then
            SSH_KEY="${candidate%.pub}"
            return 0
        fi
    done
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    info "generating a new ssh keypair at $HOME/.ssh/id_ed25519"
    ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519" -q
    SSH_KEY="$HOME/.ssh/id_ed25519"
}

# Launch bash inside a sandbox rootfs. env -i scrubs the host termux
# environment so the guest configures its own PATH/HOME/PREFIX, but the
# guest keeps termux-exec's LD_PRELOAD: termux relies on it to resolve
# /usr/bin/env shebangs (the pi wrapper and npm shims), and a real termux
# environment always carries it. proot -r makes the sandbox rootfs the
# guest's /; the only host paths shared in are /proc, /dev, /sys, and the
# read-only OS partitions (/system, /apex, /linkerconfig), which termux
# binaries need because they chain-load the Android system linker. Guest
# writes can only land in the sandbox (mount requests arrive as extra
# binds).
launch_proot() {
    local vm="$1"
    shift
    local env_args=(
        HOME="$GUEST_HOME"
        PREFIX="$GUEST_PREFIX"
        TMPDIR="$GUEST_PREFIX/tmp"
        PATH="$GUEST_PREFIX/bin"
        TERM="${TERM:-xterm-256color}"
        LD_PRELOAD="$GUEST_PREFIX/lib/libtermux-exec.so"
    )
    [ -n "${TERMUX_VERSION:-}" ] && env_args+=("TERMUX_VERSION=$TERMUX_VERSION")
    local host_binds=(-b /proc -b /dev -b /sys)
    local os_dir
    for os_dir in /system /apex /linkerconfig; do
        [ -d "$os_dir" ] && host_binds+=("-b" "$os_dir")
    done
    env -i "${env_args[@]}" \
        proot --kill-on-exit \
        -r "$(guest_root "$vm")" \
        ${host_binds[@]+"${host_binds[@]}"} \
        ${MOUNT_ARGS[@]+"${MOUNT_ARGS[@]}"} \
        -w "$GUEST_HOME" \
        "$GUEST_PREFIX/bin/bash" --login "$@"
}

run_ssh() {
    local vm="$1"
    shift
    local port
    port="$(vm_port "$vm")" || die "sandbox $vm is not running"
    ssh -p "$port" \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        -o BatchMode=yes \
        127.0.0.1 "$@"
}

# docker-cp-style copy: exactly one endpoint is prefixed in: and refers to a
# path in the sandbox (relative paths are relative to the guest home). scp -r
# copies directories recursively and is harmless for plain files.
do_cp() {
    local src="$1"
    local dst="$2"
    if [ "${src#in:}" != "$src" ] && [ "${dst#in:}" != "$dst" ]; then
        die "cp: only one endpoint may be a in: path"
    fi
    if [ "${src#in:}" = "$src" ] && [ "${dst#in:}" = "$dst" ]; then
        die "cp: one endpoint must be a in: path"
    fi
    local port
    port="$(vm_port "$VM_NAME")" || die "sandbox $VM_NAME is not running"
    if [ "${src#in:}" != "$src" ]; then
        info "copying $VM_NAME:${src#in:} to $dst"
        scp -r -P "$port" \
            -o UserKnownHostsFile="$KNOWN_HOSTS" \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=10 \
            -o BatchMode=yes \
            "127.0.0.1:${src#in:}" "$dst"
    else
        info "copying $src to $VM_NAME:${dst#in:}"
        scp -r -P "$port" \
            -o UserKnownHostsFile="$KNOWN_HOSTS" \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=10 \
            -o BatchMode=yes \
            "$src" "127.0.0.1:${dst#in:}"
    fi
}

# Interactive ssh (no BatchMode so the user can authenticate themselves).
ssh_interactive() {
    local vm="$1"
    local port
    port="$(vm_port "$vm")" || die "sandbox $vm is not running; run $0 create"
    exec ssh -t -p "$port" \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        127.0.0.1
}

wait_ssh_ready() {
    local port="$1"
    local timeout="${2:-300}"
    local waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            exec 3>&- 3<&- 2>/dev/null || true
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

# The rootfs is a plain directory, so the project transfer and the git file
# mode restore happen on the host side; only commands that need the guest
# userland (pre-commit) go through proot.
transfer_project() {
    local vm="$1"
    local excludes
    excludes="$(tar_exclude_args)"
    info "transferring $CURRENT_DIR to $vm"
    # shellcheck disable=SC2086
    tar -C "$PWD/.." -cf - $excludes "$CURRENT_DIR" |
        tar -xf - -C "$(guest_root "$vm")$GUEST_HOME/"
}

restore_git_modes() {
    local vm="$1"
    (
        cd "$(guest_root "$vm")$GUEST_HOME/$CURRENT_DIR" || exit 0
        git ls-files -s 2>/dev/null | grep '^100' |
            while read -r mode type sha path; do
                chmod "${mode:3:4}" "$path"
            done || true
    )
    if [ -f "$(guest_root "$vm")$GUEST_HOME/$CURRENT_DIR/.git/hooks/pre-commit" ]; then
        launch_proot "$vm" -c "cd ~/$CURRENT_DIR && pre-commit install" >/dev/null 2>&1 || true
    fi
}

set_ollama_url() {
    local vm="$1"
    local host="$2"
    local url
    case "$host" in
    *://*)
        url="$host"
        ;;
    *:*)
        url="http://$host"
        ;;
    *)
        # The guest shares the device's network, so the host ollama is
        # localhost from the guest's point of view.
        url="http://127.0.0.1:$host"
        ;;
    esac
    url="${url%/}"
    info "setting ollama url to $url"
    launch_proot "$vm" -c "export PATH=\"\$HOME/.local/bin:\$PATH\"; set_ollama.sh '$url'" >/dev/null ||
        die "set_ollama.sh failed in $vm"
}

# Extract the bootstrap zip and apply SYMLINKS.txt, which stores the
# rootfs's symlinks as target<-link lines because the zip format cannot
# carry them.
apply_symlinks() {
    local usr="$1" line target link
    [ -f "$usr/SYMLINKS.txt" ] || return 0
    while IFS= read -r line; do
        target="${line%%←*}"
        link="${line#*←}"
        link="${link#./}"
        [ -n "$target" ] && [ -n "$link" ] || continue
        ln -sfn "$target" "$usr/$link"
    done < "$usr/SYMLINKS.txt"
    rm -f "$usr/SYMLINKS.txt"
}

# Expand mount requests into proot -b arguments. Read-only is accepted for
# interface parity with agent_tart.sh but proot does not enforce it.
MOUNT_ARGS=()

add_mount() {
    # add_mount <host src> <rw|ro> [guest dst]
    local src="$1"
    local mode="$2"
    local dst="${3:-}"
    src="${src/#\~/$HOME}"
    if [ ! -d "$src" ]; then
        info "skipping mount of missing directory $src"
        return 0
    fi
    [ "$dst" ] || dst="$src"
    [ "$dst" != "${dst#\~}" ] && dst="$GUEST_HOME${dst#\~}"
    if [ "$mode" = ro ]; then
        info "note: proot does not enforce read-only on the bind of $src"
    fi
    # The bind point must exist inside the rootfs for proot to mount onto
    # it; guest paths map to the sandbox rootfs one-to-one.
    local host_dst="$(guest_root "$VM_NAME")$dst"
    mkdir -p "$host_dst" 2>/dev/null || true
    MOUNT_ARGS+=("-b" "$src:$dst")
    info "sharing $src as $dst"
}

build_mount_args() {
    MOUNT_ARGS=()
    if [ "$LOCAL" = 1 ]; then
        add_mount "$HOME/.vim" ro "$GUEST_HOME/.vim"
        add_mount "$HOME/.vim_backup" rw "$GUEST_HOME/.vim_backup"
        add_mount "$HOME/.pi/agent/sessions" rw "$GUEST_HOME/.pi/agent/sessions"
    fi
    local entry rest mode src dst
    for entry in ${MOUNTS[@]+"${MOUNTS[@]}"}; do
        mode="rw"
        rest="$entry"
        case "$rest" in
        *:ro)
            mode="ro"
            rest="${rest%:ro}"
            ;;
        *:rw)
            rest="${rest%:rw}"
            ;;
        esac
        case "$rest" in
        *:*)
            src="${rest%%:*}"
            dst="${rest#*:}"
            ;;
        *)
            src="$rest"
            dst=""
            ;;
        esac
        add_mount "$src" "$mode" "$dst"
    done
}

# Delete a sandbox: stop its sshd and remove the rootfs. This is the entire
# cleanup story - everything the guest ever wrote lives in the sandbox
# directory.
delete_sandbox() {
    local vm="$1"
    local pidfile="$AGENT_DIR/$vm/.sshd.pid" pid
    if [ -r "$pidfile" ]; then
        while read -r pid; do
            [ -n "$pid" ] || continue
            if kill "$pid" 2>/dev/null; then
                info "stopping $vm (pid $pid)"
                local waited=0
                while [ "$waited" -lt 5 ] && kill -0 "$pid" 2>/dev/null; do
                    sleep 1
                    waited=$((waited + 1))
                done
                kill -9 "$pid" 2>/dev/null || true
            fi
        done < "$pidfile"
        rm -f "$pidfile"
    fi
    # An orphaned sshd can outlive its proot parent (killing proot does not
    # always take the tracee tree down); sweep any stragglers before
    # removing the directory.
    pkill -f "$AGENT_DIR/$vm" 2>/dev/null || true
    if [ -d "$AGENT_DIR/$vm" ]; then
        info "deleting $vm"
        rm -rf "$AGENT_DIR/$vm" || die "could not delete $AGENT_DIR/$vm"
    fi
}

tar_exclude_args() {
    local pattern item
    for pattern in ${SKIPS[@]+"${SKIPS[@]}"}; do
        echo "$pattern" | tr ',' '\n' | while IFS= read -r item; do
            item="${item#/}"
            item="${item%/}"
            [ -n "$item" ] || continue
            case "$item" in
            */*)
                printf ' --exclude=%q' "$item"
                ;;
            *)
                printf ' --exclude=%q --exclude=*/%q --exclude=%q/* --exclude=*/%q/*' \
                    "$item" "$item" "$item" "$item"
                ;;
            esac
        done
    done
}

# Every command that talks to a sandbox over ssh needs the key resolved
# first; ensure_ssh_key is a no-op when it is already set.
case "$COMMAND" in
list | stop) ;;
*) ensure_ssh_key ;;
esac

case "$COMMAND" in
provision)
    [ -n "$BASE_IMAGE" ] && BOOTSTRAP_URL="$BASE_IMAGE"
    if [ -z "$BOOTSTRAP_URL" ]; then
        # Resolve the latest bootstrap release. The tag carries a '+' which
        # must be percent-encoded in the download url.
        tag="$(curl -fsSL https://api.github.com/repos/termux/termux-packages/releases 2>/dev/null |
            jq -r '[.[].tag_name | select(startswith("bootstrap-"))][0]')"
        [ -n "$tag" ] && [ "$tag" != "null" ] ||
            die "could not resolve the latest bootstrap release; pass --image <bootstrap zip url>"
        BOOTSTRAP_URL="https://github.com/termux/termux-packages/releases/download/$(printf '%s' "$tag" | sed 's/+/%2B/g')/bootstrap-aarch64.zip"
    fi
    zip_name="${BOOTSTRAP_URL##*/}"
    zip_name="${zip_name//%2B/+}"
    zip_path="$AGENT_DIR/$zip_name"
    check_free_space "provisioning the base"
    if [ ! -s "$zip_path" ]; then
        info "downloading $BOOTSTRAP_URL"
        curl -fsSL -o "$zip_path" "$BOOTSTRAP_URL" ||
            die "downloading the bootstrap failed; retry or pass --image <bootstrap zip url>"
    else
        info "using cached bootstrap $zip_path (delete it to re-download)"
    fi
    if vm_exists "$BASE_NAME"; then
        info "$BASE_NAME exists; deleting it for a fresh provisioning"
        delete_sandbox "$BASE_NAME"
    fi
    root="$(guest_root "$BASE_NAME")"
    mkdir -p "$root$GUEST_PREFIX" "$root$GUEST_HOME"
    info "extracting the bootstrap into $root$GUEST_PREFIX"
    unzip -q "$zip_path" -d "$root$GUEST_PREFIX" ||
        die "extracting $zip_path failed"
    apply_symlinks "$root$GUEST_PREFIX"
    # Termux's apt keeps its download cache outside \$PREFIX, at the app's
    # cache dir; create it inside the rootfs so the guest's package
    # operations never touch the host termux's apt cache.
    mkdir -p "$root/data/data/com.termux/cache/apt/archives/partial"
    if [ -f "$SELF_DIR/install_pi.sh" ]; then
        cp "$SELF_DIR/install_pi.sh" "$root$GUEST_HOME/"
    else
        die "install_pi.sh not found next to agent_termux.sh"
    fi
    info "provisioning the base (installs node and pi; takes a while; log $LOG_DIR/$BASE_NAME.log)"
    launch_proot "$BASE_NAME" -c '
        pkg install -y git openssh &&
        curl -fsSL "https://github.com/hadolint/hadolint/releases/latest/download/hadolint-Linux-$(uname -m | sed "s/aarch64/arm64/")" -o "$PREFIX/bin/hadolint" &&
        chmod +x "$PREFIX/bin/hadolint" &&
        bash ~/install_pi.sh &&
        ssh-keygen -A >/dev/null &&
        rm -f ~/install_pi.sh
    ' >>"$LOG_DIR/$BASE_NAME.log" 2>&1 ||
        die "provisioning failed; $BASE_NAME left in place for debugging"
    # Stable per-device ssh host keys: generated once and installed into
    # every base (after openssh exists, which creates usr/etc/ssh), so
    # sandboxes cloned from it share one identity and known_hosts entries
    # survive re-provisioning (a fresh bootstrap would otherwise mint new
    # keys and break every stored entry). These overwrite whatever the
    # guest's ssh-keygen -A above generated.
    keydir="$SSH_DIR/hostkeys"
    mkdir -p "$keydir"
    if [ ! -e "$keydir/ssh_host_ed25519_key" ]; then
        info "generating sandbox ssh host keys in $keydir"
        for keytype in ed25519 ecdsa rsa; do
            ssh-keygen -t "$keytype" -N '' -f "$keydir/ssh_host_${keytype}_key" -q
        done
    fi
    cp -f "$keydir"/ssh_host_* "$root$GUEST_PREFIX/etc/ssh/" ||
        die "could not install the cached ssh host keys into the base"
    chmod 600 "$root$GUEST_PREFIX/etc/ssh/ssh_host"_*_key
    info "done. Create sandboxes with: $0 create"
    ;;

create | start)
    vm_exists "$BASE_NAME" || die "base $BASE_NAME does not exist; run $0 provision first"
    # Like agent_docker.py, create and start always destroy an existing
    # sandbox and rebuild it from the base; state worth keeping belongs on
    # the host, in the project tree, or is pulled out with cp first.
    if vm_exists "$VM_NAME"; then
        info "removing existing sandbox $VM_NAME"
        delete_sandbox "$VM_NAME"
    fi
    info "cloning $BASE_NAME into $VM_NAME"
    check_free_space "cloning the base"
    cp -a "$AGENT_DIR/$BASE_NAME" "$AGENT_DIR/$VM_NAME" ||
        die "cloning $BASE_NAME failed"
    # The base carries the host's public key from provisioning; --sshkey
    # replaces it for this sandbox.
    mkdir -p "$(guest_root "$VM_NAME")$GUEST_HOME/.ssh"
    chmod 700 "$(guest_root "$VM_NAME")$GUEST_HOME/.ssh"
    [ -n "$SSH_PUB" ] && SSH_KEY="${SSH_PUB%.pub}"
    cat "$SSH_KEY.pub" > "$(guest_root "$VM_NAME")$GUEST_HOME/.ssh/authorized_keys"
    chmod 600 "$(guest_root "$VM_NAME")$GUEST_HOME/.ssh/authorized_keys"
    build_mount_args
    transfer_project "$VM_NAME"
    restore_git_modes "$VM_NAME"
    [ -e "$(guest_root "$VM_NAME")$GUEST_PREFIX/etc/ssh/ssh_host_ed25519_key" ] ||
        launch_proot "$VM_NAME" -c "ssh-keygen -A" >/dev/null 2>&1
    PORT="$(free_port "$SSH_PORT_BASE")"
    info "starting $VM_NAME sshd on 127.0.0.1:$PORT (log $LOG_DIR/$VM_NAME.log)"
    # Backgrounded function call: bash forks a subshell whose pid becomes
    # env's and then proot's through exec, so the recorded pid is proot's.
    launch_proot "$VM_NAME" -c "sshd -D -p $PORT -o ListenAddress=127.0.0.1" \
        >>"$LOG_DIR/$VM_NAME.log" 2>&1 &
    echo $! > "$AGENT_DIR/$VM_NAME/.sshd.pid"
    echo "$PORT" > "$AGENT_DIR/$VM_NAME/.sshd.port"
    # Probe in short intervals and fail fast if the sshd process dies instead
    # of waiting out the full timeout on a hopeless start. Liveness uses
    # kill -0 on the recorded pid rather than vm_running: right after the
    # launch the pid is still the forked subshell before it execs proot, so
    # the cmdline-based check can spuriously fail here.
    read -r launcher_pid < "$AGENT_DIR/$VM_NAME/.sshd.pid"
    waited=0
    while [ "$waited" -lt 300 ]; do
        kill -0 "$launcher_pid" 2>/dev/null ||
            die "sshd for $VM_NAME exited; check $LOG_DIR/$VM_NAME.log"
        if wait_ssh_ready "$PORT" 5; then
            break
        fi
        waited=$((waited + 5))
    done
    if [ "$waited" -ge 300 ]; then
        die "$VM_NAME did not become reachable on port $PORT; check $LOG_DIR/$VM_NAME.log"
    fi
    # Record the guest sshd's own pid alongside proot's: sshd's command line
    # does not mention the sandbox, so it could not be swept by name if proot
    # were killed without taking the tracee tree down. Anchor at the cmdline
    # start so proot's own arguments (which contain the same sshd string) do
    # not match.
    sshd_pid="$(pgrep -f "^sshd -D -p $PORT " | head -n1)"
    [ -n "$sshd_pid" ] && echo "$sshd_pid" >> "$AGENT_DIR/$VM_NAME/.sshd.pid"
    [ -z "$OLLAMA" ] || set_ollama_url "$VM_NAME" "$OLLAMA"
    if [ "$COMMAND" = create ]; then
        ssh_interactive "$VM_NAME"
    else
        info "sandbox ready; connect with: $0 exec --name $VM_NAME"
    fi
    ;;

stop)
    # Docker semantics: stopping a sandbox destroys it.
    delete_sandbox "$VM_NAME"
    ;;

exec)
    vm_running "$VM_NAME" || die "$VM_NAME is not running; run $0 create"
    ssh_interactive "$VM_NAME"
    ;;

update)
    vm_running "$VM_NAME" || die "$VM_NAME is not running"
    [ -n "$OLLAMA" ] || die "update requires --ollama/--url"
    set_ollama_url "$VM_NAME" "$OLLAMA"
    ;;

list)
    for sbx in "$AGENT_DIR"/*/; do
        sbx="${sbx%/}"
        name="${sbx##*/}"
        [ "$name" = "$BASE_NAME" ] && continue
        vm_exists "$name" || continue
        if vm_running "$name"; then
            printf '%s\trunning\tport %s\n' "$name" "$(vm_port "$name")"
        else
            printf '%s\tstopped\n' "$name"
        fi
    done
    ;;

cp)
    if [ "${#POSITIONALS[@]}" -ne 2 ]; then
        usage
    fi
    do_cp "${POSITIONALS[${#POSITIONALS[@]} - 2]}" "${POSITIONALS[${#POSITIONALS[@]} - 1]}"
    ;;
esac
