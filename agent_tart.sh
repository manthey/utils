#!/usr/bin/env bash
# agent_tart.sh - tart-based macOS equivalent of agent_docker.py.
#
# agent_docker.py builds agentic-programming sandboxes (running pi) from the
# agent.Dockerfile docker image. This script does the same on an Apple
# Silicon Mac with tart VMs, with the same command set and semantics as
# agent_docker.py (sandboxes are ephemeral: stop destroys the VM, and start
# always rebuilds one from the base) plus a cp command:
#
#   provision  create the base VM (equivalent to building the docker image)
#   create     clone the base VM, copy the project in, shell in
#   start      like create, but does not shell in
#   stop       destroy the sandbox VM
#   exec       interactive shell in a running sandbox
#   update     apply --ollama to a running sandbox
#   list       list known sandboxes
#   ip         print the VM IP
#   cp         copy files to/from the VM; one endpoint prefixed in:
#   claudeout  run install_claude.sh update in the VM and copy the
#              files that changed back to the --claude directory
#
# --claude=DIR names a local .claude-persist directory (containing
# install_claude.sh; --claude= means the claude directory next to this
# script); create and start copy it to ~/.claude-persist in the VM and
# run install_claude.sh there. Without --claude, Claude is not installed.
#
# cp usage: agent_tart.sh cp [--name NAME] in:<guest path> <local path>
#           agent_tart.sh cp [--name NAME] <local path> in:<guest path>
#           (like docker cp; directories are copied recursively)
#
# The in-VM provisioning lives in agent_tart_provision.sh; pi is installed by
# install_pi.sh (shared with the docker flow). mini-swe-agent support from
# agent.Dockerfile is intentionally omitted.
#
# Host requirements:
#   brew install jq sshpass bash
#   brew tap openai/tools && brew install openai/tools/tart

set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${TART_LOG_DIR:-$HOME/.agent_tart/logs}"
SSH_DIR="${TART_SSH_DIR:-$HOME/.agent_tart/ssh}"

BASE_VM="${TART_BASE_VM:-agent_base}"
BASE_IMAGE="${TART_BASE_IMAGE:-ghcr.io/cirruslabs/macos-golden-gate-base:latest}"
TART_USER="${TART_USER:-admin}"
TART_PASS="${TART_PASS:-admin}"
GUEST_HOME="/Users/$TART_USER"
CPUS=4
MEM=8192
DISK=150
# Use more of a larger host: all but one of its CPUs and, since tart runs at
# most two VMs at once, a third of its memory.
HOST_CPUS="$(sysctl -n hw.ncpu 2>/dev/null)" || HOST_CPUS=0
HOST_MEM="$(sysctl -n hw.memsize 2>/dev/null)" || HOST_MEM=0
[ $((${HOST_CPUS:-0} - 1)) -gt "$CPUS" ] && CPUS=$((HOST_CPUS - 1))
[ $((${HOST_MEM:-0} / 1048576 / 3)) -gt "$MEM" ] && MEM=$((HOST_MEM / 1048576 / 3))

BASE_NAME="$BASE_VM"
NAME=""
NUM=""
SRC=""
OVERRIDE_IMAGE=""
SSH_KEY=""
SSH_PUB=""
COMMAND=""
LOCAL=0
OLLAMA=""
CLAUDE_DIR=""
CONFIG_FILE="$SELF_DIR/agent_tart.yaml"
MOUNTS=()
SKIPS=()
POSITIONALS=()

die() {
    echo "agent_tart: $*" >&2
    exit 1
}

info() {
    echo "agent_tart: $*" >&2
}

usage() {
    # Print the header comment block as the help text; keep the range in sync
    # with the header above.
    sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

safe_name() {
    printf '%s' "$1" | sed 's/[<>:"/\\|?*[:cntrl:]]/_/g; s/[. ]*$//; s/^$/docker/'
}

parse_args() {
    MOUNTS=()
    SKIPS=()
    while [ $# -gt 0 ]; do
        case "$1" in
        --*=*)
            # Accept the --option=value spelling, like argparse does in
            # agent_docker.py, by splitting the argument into two words.
            if [ $# -gt 1 ]; then
                set -- "${1%%=*}" "${1#*=}" "${@:2}"
            else
                set -- "${1%%=*}" "${1#*=}"
            fi
            ;;
        esac
        case "$1" in
        provision | create | start | stop | exec | update | list | ip | cp | claudeout)
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
            OVERRIDE_IMAGE="$2"
            shift
            ;;
        --cpus)
            CPUS="$2"
            shift
            ;;
        --memory)
            MEM="$2"
            shift
            ;;
        --disk)
            DISK="$2"
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
        --claude)
            # --claude= (an empty value) means the claude directory next to
            # this script.
            CLAUDE_DIR="${2:-$SELF_DIR/claude}"
            shift
            ;;
        --no-claude)
            CLAUDE_DIR=""
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
# agent_docker.py: arguments merge as "all" entries, then per-directory
# entries, then command-line arguments (later scalars win; --mount/--skip
# accumulate from every source).
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
if [ "$COMMAND" != "cp" ]; then
    apply_config
fi

if [ -n "$SRC" ]; then
    cd "$SRC" || die "cannot cd to $SRC"
fi
if [ -n "$CLAUDE_DIR" ]; then
    CLAUDE_DIR="${CLAUDE_DIR/#\~/$HOME}"
    [ "${CLAUDE_DIR#/}" != "$CLAUDE_DIR" ] || CLAUDE_DIR="$PWD/$CLAUDE_DIR"
fi

for tool in tart jq ssh; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found; see agent_tart.md host prerequisites"
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
    tart list --quiet 2>/dev/null | grep -Fxq "$1"
}

vm_running() {
    if tart list --json 2>/dev/null | jq -e --arg n "$1" '.[] | select(.Name == $n and .Running)' >/dev/null 2>&1; then
        return 0
    fi
    # Cross-version fallback: tart ip only succeeds for a running VM.
    tart ip "$1" >/dev/null 2>&1
}

vm_ip() {
    # shellcheck disable=SC2086
    tart ip "$1" --wait "${2:-120}" 2>/dev/null
}

# Wait until the VM actually accepts TCP connections on the ssh port. A VM
# cloned from a base reuses the base's MAC address, so a stale DHCP lease can
# make `tart ip` return an address seconds before the guest is reachable.
wait_ssh_ready() {
    local vm="$1"
    local timeout="${2:-300}"
    local waited=0
    local ip
    while [ "$waited" -lt "$timeout" ]; do
        if ip="$(tart ip "$vm" 2>/dev/null)" && [ -n "$ip" ]; then
            if (exec 3<>"/dev/tcp/$ip/22") 2>/dev/null; then
                return 0
            fi
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 1
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

# Run a command in the VM via key-based ssh.
run_ssh() {
    local vm="$1"
    shift
    local ip
    ip="$(vm_ip "$vm" 10)" || die "no IP for VM $vm (is it running?)"
    ssh -i "$SSH_KEY" \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        -o BatchMode=yes \
        "$TART_USER@$ip" "$@"
}

run_scp() {
    local vm="$1"
    local src="$2"
    local dst="$3"
    local ip
    ip="$(vm_ip "$vm" 10)" || die "no IP for VM $vm (is it running?)"
    scp -i "$SSH_KEY" \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        -o BatchMode=yes \
        "$src" "$TART_USER@$ip:$dst"
}

# docker-cp-style copy: exactly one endpoint is prefixed in: and refers to a
# path in the VM (relative paths are relative to the guest home). scp -r
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
    if ! vm_exists "$VM_NAME"; then
        die "VM $VM_NAME does not exist; run $0 create in the project directory (or use --src)"
    fi
    local ip
    ip="$(vm_ip "$VM_NAME" 10)" || die "no IP for VM $VM_NAME (is it running?)"
    if [ "${src#in:}" != "$src" ]; then
        info "copying $VM_NAME:${src#in:} to $dst"
        scp -r -i "$SSH_KEY" \
            -o UserKnownHostsFile="$KNOWN_HOSTS" \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=10 \
            -o BatchMode=yes \
            "$TART_USER@$ip:${src#in:}" "$dst"
    else
        info "copying $src to $VM_NAME:${dst#in:}"
        scp -r -i "$SSH_KEY" \
            -o UserKnownHostsFile="$KNOWN_HOSTS" \
            -o StrictHostKeyChecking=no \
            -o ConnectTimeout=10 \
            -o BatchMode=yes \
            "$src" "$TART_USER@$ip:${dst#in:}"
    fi
}

# Interactive ssh (no BatchMode so the user can authenticate themselves).
ssh_interactive() {
    local vm="$1"
    local ip
    ip="$(vm_ip "$vm" 120)" || die "no IP for VM $vm (is it running?)"
    exec ssh -t \
        -i "$SSH_KEY" \
        -o UserKnownHostsFile="$KNOWN_HOSTS" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "$TART_USER@$ip"
}

stop_vm() {
    local vm="$1"
    vm_exists "$vm" || return 0
    info "stopping $vm"
    # Attempt the graceful stop regardless of what vm_running reports; the
    # running-state detection is not reliable across tart versions and states.
    tart stop --timeout 60 "$vm" >/dev/null 2>&1 ||
        info "tart stop did not report success for $vm (it may not be running)"
    if vm_running "$vm"; then
        info "$vm still appears to be running; terminating its process"
        pkill -f "tart run.* $vm\$" >/dev/null 2>&1 || true
        sleep 2
    fi
}

delete_vm() {
    local vm="$1"
    stop_vm "$vm"
    vm_exists "$vm" || return 0
    info "deleting $vm"
    if ! tart delete "$vm" 2>/dev/null; then
        info "$vm is busy; terminating its tart process and retrying"
        pkill -f "tart run.* $vm\$" >/dev/null 2>&1 || true
        sleep 2
        tart delete "$vm" || die "could not delete $vm"
    fi
    vm_exists "$vm" && die "$vm still exists after delete"
    info "deleted $vm"
}

# One-time bootstrap of the ssh key into a fresh base VM using the published
# image credentials (admin/admin).
bootstrap_ssh_key() {
    local vm="$1"
    info "waiting for $vm to accept ssh connections"
    wait_ssh_ready "$vm" 300 ||
        die "$vm did not become reachable over ssh; check $LOG_DIR/$vm.log"
    if run_ssh "$vm" true 2>/dev/null; then
        return 0
    fi
    ensure_ssh_key
    info "installing $SSH_KEY.pub into $vm (password bootstrap)"
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "$TART_PASS" ssh-copy-id \
            -i "$SSH_KEY.pub" \
            -o UserKnownHostsFile="$KNOWN_HOSTS" \
            -o StrictHostKeyChecking=no \
            "$TART_USER@$(vm_ip "$vm")" ||
            die "could not install the ssh key into $vm"
    else
        command -v expect >/dev/null 2>&1 || die "install sshpass or expect to bootstrap the ssh key"
        expect -c "
            set timeout 60
            spawn ssh -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=no \
                $TART_USER@$(vm_ip "$vm") \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\"
            expect {
                -re \"(P|p)assword:\" { send \"$TART_PASS\r\"; exp_continue }
                eof
            }
        " || die "could not install the ssh key into $vm"
    fi
    run_ssh "$vm" true 2>/dev/null ||
        die "ssh into $vm still fails after the key bootstrap"
}

# Expand mount requests into tart --dir arguments plus guest-side symlinks
# that expose each share at its docker-style guest path. virtiofs shares are
# attached at tart run time, so this must happen before the VM starts.
MOUNT_ARGS=()
MOUNT_TAGS=()
MOUNT_DSTS=()
MOUNT_COUNT=0

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
    MOUNT_COUNT=$((MOUNT_COUNT + 1))
    local tag="mnt$MOUNT_COUNT"
    [ "$dst" ] || dst="$src"
    if [ "$mode" = ro ]; then
        MOUNT_ARGS+=("--dir=$tag:$src:ro")
    else
        MOUNT_ARGS+=("--dir=$tag:$src")
    fi
    MOUNT_TAGS+=("$tag")
    MOUNT_DSTS+=("$dst")
    info "sharing $src as $dst ($tag, $mode)"
}

build_mount_args() {
    MOUNT_ARGS=()
    MOUNT_TAGS=()
    MOUNT_DSTS=()
    MOUNT_COUNT=0
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

# After boot, expose virtiofs shares (which appear under
# /Volumes/My Shared Files/<tag> in a macOS guest) at their intended paths.
link_mounts() {
    local vm="$1"
    [ "$MOUNT_COUNT" -gt 0 ] || return 0
    local i=0
    while [ "$i" -lt "$MOUNT_COUNT" ]; do
        local tag="${MOUNT_TAGS[$i]}"
        local dst="${MOUNT_DSTS[$i]}"
        run_ssh "$vm" "mkdir -p \"\$(dirname '$dst')\" && rm -rf '$dst' && \
            ln -sfn '/Volumes/My Shared Files/$tag' '$dst'" || true
        i=$((i + 1))
    done
}

start_vm() {
    local vm="$1"
    shift
    mkdir -p "$LOG_DIR"
    if vm_running "$vm"; then
        return 0
    fi
    info "starting $vm (log $LOG_DIR/$vm.log)"
    nohup tart run --no-graphics "$@" "$vm" >>"$LOG_DIR/$vm.log" 2>&1 &
    vm_ip "$vm" 600 >/dev/null || die "VM $vm did not acquire an IP; check $LOG_DIR/$vm.log"
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

transfer_project() {
    local vm="$1"
    local excludes
    excludes="$(tar_exclude_args)"
    info "transferring $CURRENT_DIR to $VM_NAME"
    # shellcheck disable=SC2086
    tar -C "$PWD/.." -cf - $excludes "$CURRENT_DIR" | run_ssh "$vm" "tar -xf - -C ~/"
}

restore_git_modes() {
    local vm="$1"
    run_ssh "$vm" bash -s <<EOF
cd ~/$CURRENT_DIR && git ls-files -s 2>/dev/null | grep '^100' |
while read mode type sha path; do chmod "\${mode:3:4}" "\$path"; done || true
if [ -f .git/hooks/pre-commit ]; then pre-commit install || true; fi
EOF
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
        local gateway
        gateway="$(run_ssh "$vm" "netstat -nr | awk '/default/{print \$2; exit}'")"
        url="http://$gateway:$host"
        ;;
    esac
    url="${url%/}"
    info "setting ollama url to $url"
    run_ssh "$vm" "set_ollama.sh '$url'"
}

# Copy the --claude directory to ~/.claude-persist in the VM and run its
# install_claude.sh. COPYFILE_DISABLE keeps macOS tar from adding ._ files.
claude_in() {
    local vm="$1"
    [ -f "$CLAUDE_DIR/install_claude.sh" ] || die "$CLAUDE_DIR does not contain install_claude.sh"
    info "copying $CLAUDE_DIR to $vm:~/.claude-persist"
    COPYFILE_DISABLE=1 tar -C "$CLAUDE_DIR" -cf - . |
        run_ssh "$vm" "mkdir -p ~/.claude-persist && tar -xf - -C ~/.claude-persist" ||
        die "copying $CLAUDE_DIR failed"
    run_ssh "$vm" "bash ~/.claude-persist/install_claude.sh" ||
        info "install_claude.sh failed"
}

# Run install_claude.sh update in the VM and copy ~/.claude-outbox back to
# the --claude directory; install_claude.sh puts the files it changed there.
claude_out() {
    local vm="$1"
    run_ssh "$vm" "bash ~/.claude-persist/install_claude.sh update" ||
        die "install_claude.sh update failed"
    mkdir -p "$CLAUDE_DIR"
    info "copying $vm:~/.claude-outbox to $CLAUDE_DIR"
    run_ssh "$vm" "COPYFILE_DISABLE=1 tar -C ~/.claude-outbox -cf - ." |
        tar -xf - -C "$CLAUDE_DIR" ||
        die "copying ~/.claude-outbox failed"
}

# Every command that talks to a VM over ssh needs the key resolved first;
# ensure_ssh_key is a no-op when it is already set.
case "$COMMAND" in
list | stop | ip) ;;
*) ensure_ssh_key ;;
esac

case "$COMMAND" in
provision)
    [ -n "$OVERRIDE_IMAGE" ] && BASE_IMAGE="$OVERRIDE_IMAGE"
    # Provisioning is defined entirely by agent_tart_provision.sh, so the base
    # is always rebuilt from the pristine image; there is deliberately no
    # in-place update mode. If a hand-modified base is worth keeping, rename
    # or push it first: tart rename agent_base agent_base_experiment.
    if vm_exists "$BASE_NAME"; then
        info "$BASE_NAME exists; deleting it for a fresh provisioning"
        delete_vm "$BASE_NAME"
    fi
    vm_exists "$BASE_NAME" &&
        die "$BASE_NAME could not be removed; refusing to provision over an existing VM"
    info "cloning $BASE_IMAGE into $BASE_NAME (large download on first run)"
    tart clone "$BASE_IMAGE" "$BASE_NAME" || die "cloning $BASE_IMAGE failed"
    tart set "$BASE_NAME" --cpu "$CPUS" --memory "$MEM" --disk-size "$DISK" ||
        die "configuring $BASE_NAME failed"
    ensure_ssh_key
    start_vm "$BASE_NAME"
    bootstrap_ssh_key "$BASE_NAME"
    run_ssh "$BASE_NAME" "mkdir -p '$GUEST_HOME/.agent_tart_provision'"
    for f in agent_tart_provision.sh install_pi.sh .vimrc cover.py; do
        if [ -f "$SELF_DIR/$f" ]; then
            run_scp "$BASE_NAME" "$SELF_DIR/$f" ".agent_tart_provision/$f" ||
                die "could not copy $f to $BASE_NAME"
        fi
    done
    info "provisioning (this compiles several pythons; it takes a while)"
    if ! run_ssh "$BASE_NAME" "bash ~/.agent_tart_provision/agent_tart_provision.sh"; then
        die "provisioning failed; $BASE_NAME left running for debugging"
    fi
    info "shutting down $BASE_NAME"
    stop_vm "$BASE_NAME"
    info "done. Create sandboxes with: $0 create"
    ;;

create | start)
    vm_exists "$BASE_NAME" || die "base VM $BASE_NAME does not exist; run $0 provision first"
    if vm_running "$BASE_NAME"; then
        info "base VM $BASE_NAME is running; stopping it so it can be cloned"
        stop_vm "$BASE_NAME"
    fi
    # Like agent_docker.py, create and start always destroy an existing
    # sandbox and rebuild it from the base; state worth keeping belongs on
    # the host, in the project tree, or is pulled out with cp first.
    if vm_exists "$VM_NAME"; then
        info "removing existing VM $VM_NAME"
        delete_vm "$VM_NAME"
    fi
    info "cloning $BASE_NAME into $VM_NAME"
    tart clone "$BASE_NAME" "$VM_NAME" || die "cloning $BASE_NAME failed"
    tart set "$VM_NAME" --cpu "$CPUS" --memory "$MEM" ||
        die "configuring $VM_NAME failed"
    tart set "$VM_NAME" --disk-size "$DISK" 2>/dev/null || true
    build_mount_args
    start_vm "$VM_NAME" ${MOUNT_ARGS[@]+"${MOUNT_ARGS[@]}"}
    info "waiting for $VM_NAME to accept ssh connections"
    wait_ssh_ready "$VM_NAME" 300 ||
        die "$VM_NAME did not become reachable over ssh; check $LOG_DIR/$VM_NAME.log"
    if [ -n "$SSH_PUB" ]; then
        ensure_ssh_key
        run_ssh "$VM_NAME" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" \
            <"$SSH_PUB"
    fi
    link_mounts "$VM_NAME"
    transfer_project "$VM_NAME"
    restore_git_modes "$VM_NAME"
    [ -z "$OLLAMA" ] || set_ollama_url "$VM_NAME" "$OLLAMA"
    [ -z "$CLAUDE_DIR" ] || claude_in "$VM_NAME"
    if [ "$COMMAND" = create ]; then
        ssh_interactive "$VM_NAME"
    else
        info "sandbox ready; connect with: $0 exec --name $VM_NAME"
    fi
    ;;

stop)
    # Docker semantics: stopping a sandbox destroys it.
    delete_vm "$VM_NAME"
    ;;

exec)
    ssh_interactive "$VM_NAME"
    ;;

update)
    vm_running "$VM_NAME" || die "$VM_NAME is not running"
    [ -n "$OLLAMA" ] || die "update requires --ollama/--url"
    set_ollama_url "$VM_NAME" "$OLLAMA"
    ;;

list)
    tart list --json 2>/dev/null | jq -r '
        .[] | select(.Source == "local")
        | [.Name, (if .Running then "running" else "stopped" end), .State, (.Size // "")]
        | @tsv'
    ;;

ip)
    vm_ip "$VM_NAME" 5 || die "no IP for $VM_NAME"
    ;;

cp)
    if [ "${#POSITIONALS[@]}" -ne 2 ]; then
        usage
    fi
    do_cp "${POSITIONALS[${#POSITIONALS[@]} - 2]}" "${POSITIONALS[${#POSITIONALS[@]} - 1]}"
    ;;

claudeout)
    [ -n "$CLAUDE_DIR" ] || die "claudeout requires --claude"
    vm_running "$VM_NAME" || die "$VM_NAME is not running"
    claude_out "$VM_NAME"
    ;;
esac
