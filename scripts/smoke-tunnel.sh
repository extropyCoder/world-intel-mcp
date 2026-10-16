#!/usr/bin/env bash
# Run on the LOCAL browser machine, not inside the remote SSH session.
set -euo pipefail
target=""
local_port=""
remote_port=""
usage() {
    echo "Usage: $0 [user@host] [--local-port 18501] [--remote-port 8501]"
    echo "Run locally. Prompts for missing settings on a terminal; Ctrl+C closes the tunnel."
}
fail() { echo "$*" >&2; exit 2; }
port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] || fail "Port must be an integer from 1 to 65535."
    (( 10#$1 >= 1 && 10#$1 <= 65535 )) || fail "Port must be from 1 to 65535."
}
while (( $# )); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --local-port|--remote-port)
            (( $# >= 2 )) || fail "Missing value for $1"
            if [[ "$1" == --local-port ]]; then local_port="$2"; else remote_port="$2"; fi
            shift 2 ;;
        -*) fail "Unknown option: $1" ;;
        *) [[ -z "$target" ]] || fail "Only one SSH destination is allowed."
           target="$1"; shift ;;
    esac
done
if [[ -t 0 ]]; then
    if [[ -z "$target" ]]; then read -r -p "SSH user@host (or SSH host alias): " target; fi
    if [[ -z "$remote_port" ]]; then read -r -p "Remote dashboard port [8501]: " remote_port; fi
    if [[ -z "$local_port" ]]; then read -r -p "Local browser port [18501]: " local_port; fi
fi
[[ "$target" =~ ^([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail "Use a plain SSH hostname/IPv4/alias, optionally user@host; no options, URLs or shell syntax."
[[ ${#target} -le 255 ]] || fail "SSH destination is too long."
local_port="${local_port:-18501}"
remote_port="${remote_port:-8501}"
port "$local_port"
port "$remote_port"
local_port="$((10#$local_port))"
remote_port="$((10#$remote_port))"
command -v ssh >/dev/null 2>&1 || fail "Install an OpenSSH client on this local browser machine."
echo "Keep scripts/smoke-test.sh running on the remote machine."
echo "Open locally: http://127.0.0.1:${local_port}/"
echo "SSH may prompt for authentication/host-key verification. Ctrl+C closes only this tunnel."
exec ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 -o PermitLocalCommand=no \
    -L "127.0.0.1:${local_port}:127.0.0.1:${remote_port}" -- "$target"
