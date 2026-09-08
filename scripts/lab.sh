#!/usr/bin/env sh
set -eu

error() {
    printf 'ERROR %s: %s\n' "$1" "$2" >&2
    exit 2
}

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
python="$root/.venv/bin/python"
uid=$(id -u)
checksum_output=$(printf '%s' "$root" | cksum)
project_id=${checksum_output%% *}
state_dir="/tmp/toolcall-replay-lab-$uid-$project_id"
state_file="$state_dir/process.state"
log_file="$state_dir/server.log"

ensure_state_dir() {
    umask 077
    if [ -L "$state_dir" ] || { [ -e "$state_dir" ] && [ ! -d "$state_dir" ]; }; then
        error STATE_INVALID "local state directory is invalid"
    fi
    if [ ! -d "$state_dir" ]; then
        mkdir -m 700 -- "$state_dir" || error STATE_INVALID "local state directory cannot be created"
    fi
    owner=$(stat -c '%u' -- "$state_dir") || error STATE_INVALID "local state directory is invalid"
    mode=$(stat -c '%a' -- "$state_dir") || error STATE_INVALID "local state directory is invalid"
    if [ "$owner" != "$uid" ] || [ "$mode" != 700 ]; then
        error STATE_INVALID "local state directory is invalid"
    fi
}

read_state() {
    if [ -L "$state_file" ] || [ ! -f "$state_file" ]; then
        error STATE_INVALID "local process state is invalid"
    fi
    state_size=$(wc -c < "$state_file") || error STATE_INVALID "local process state is invalid"
    case "$state_size" in
        ''|*[!0-9]*) error STATE_INVALID "local process state is invalid" ;;
    esac
    if [ "$state_size" -gt 1024 ]; then
        error STATE_INVALID "local process state is invalid"
    fi
    saved_pid=$(sed -n '1s/^pid=//p' "$state_file")
    saved_port=$(sed -n '2s/^port=//p' "$state_file")
    saved_ticks=$(sed -n '3s/^start_ticks=//p' "$state_file")
    saved_executable=$(sed -n '4s/^executable=//p' "$state_file")
    case "$saved_pid:$saved_port:$saved_ticks" in
        *[!0-9:]*) error STATE_INVALID "local process state is invalid" ;;
    esac
    if [ -z "$saved_pid" ] || [ -z "$saved_port" ] || [ -z "$saved_ticks" ] || [ -z "$saved_executable" ]; then
        error STATE_INVALID "local process state is invalid"
    fi
    actual_state=$(cat -- "$state_file")
    expected_state=$(printf 'pid=%s\nport=%s\nstart_ticks=%s\nexecutable=%s\n' \
        "$saved_pid" "$saved_port" "$saved_ticks" "$saved_executable")
    if [ "$actual_state" != "$expected_state" ]; then
        error STATE_INVALID "local process state is invalid"
    fi
}

process_matches() {
    kill -0 "$saved_pid" 2>/dev/null || return 1
    [ -r "/proc/$saved_pid/stat" ] || return 1
    [ -r "/proc/$saved_pid/cmdline" ] || return 1
    [ -L "/proc/$saved_pid/exe" ] || return 1
    current_ticks=$(awk '{print $22}' "/proc/$saved_pid/stat" 2>/dev/null) || return 1
    current_executable=$(readlink -- "/proc/$saved_pid/exe" 2>/dev/null) || return 1
    current_command=$(tr '\000' '\n' 2>/dev/null < "/proc/$saved_pid/cmdline") || return 1
    expected_command=$(printf '%s\n-I\n-m\ntoolcall_replay_lab.server\n--host\n127.0.0.1\n--port\n%s\n' \
        "$python" "$saved_port")
    [ "$current_ticks" = "$saved_ticks" ] && \
        [ "$current_executable" = "$saved_executable" ] && \
        [ "$current_command" = "$expected_command" ]
}

health_check() {
    "$python" -I - "$port" >/dev/null 2>&1 <<'PY'
import http.client
import sys

connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=0.1)
try:
    connection.request("GET", "/healthz", headers={"Connection": "close"})
    response = connection.getresponse()
    payload = response.read(513)
    healthy = (
        response.status == 200
        and response.getheader("Content-Type") == "application/json; charset=utf-8"
        and payload == b'{"status":"ok"}'
    )
except OSError:
    healthy = False
finally:
    connection.close()
raise SystemExit(0 if healthy else 1)
PY
}

validate_port() {
    case "$1" in
        ''|*[!0-9]*) error ARGUMENT_INVALID "port must be an integer from 1024 to 65535" ;;
    esac
    if [ "${#1}" -gt 5 ] || [ "$1" -lt 1024 ] || [ "$1" -gt 65535 ]; then
        error ARGUMENT_INVALID "port must be an integer from 1024 to 65535"
    fi
}

start_lab() {
    if [ "$#" -gt 1 ]; then
        error ARGUMENT_INVALID "usage: scripts/lab.sh start [port]"
    fi
    port=${1:-4173}
    validate_port "$port"
    [ -x "$python" ] || error RUNTIME_MISSING "development Python is unavailable"
    ensure_state_dir
    if [ -e "$state_file" ] || [ -L "$state_file" ]; then
        read_state
        if process_matches; then
            error ALREADY_RUNNING "local trace lab is already running"
        fi
        if kill -0 "$saved_pid" 2>/dev/null; then
            error STATE_INVALID "refusing to replace unrecognized process state"
        fi
        rm -f -- "$state_file"
    fi
    if [ -L "$log_file" ] || { [ -e "$log_file" ] && [ ! -f "$log_file" ]; }; then
        error STATE_INVALID "local log path is invalid"
    fi
    if [ -f "$log_file" ]; then
        log_owner=$(stat -c '%u' -- "$log_file") || error STATE_INVALID "local log path is invalid"
        log_mode=$(stat -c '%a' -- "$log_file") || error STATE_INVALID "local log path is invalid"
        log_links=$(stat -c '%h' -- "$log_file") || error STATE_INVALID "local log path is invalid"
        if [ "$log_owner" != "$uid" ] || [ "$log_mode" != 600 ] || [ "$log_links" != 1 ]; then
            error STATE_INVALID "local log path is invalid"
        fi
    fi
    : > "$log_file"
    chmod 600 -- "$log_file"
    cd -- "$root"
    "$python" -I -m toolcall_replay_lab.server --host 127.0.0.1 --port "$port" \
        >> "$log_file" 2>&1 &
    pid=$!
    attempts=0
    while ! health_check; do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            error START_FAILED "local trace lab did not start"
        fi
        if [ "$attempts" -ge 40 ]; then
            kill -TERM "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            error START_FAILED "local trace lab did not become healthy"
        fi
        attempts=$((attempts + 1))
        sleep 0.05
    done
    start_ticks=$(awk '{print $22}' "/proc/$pid/stat") || {
        kill "$pid" 2>/dev/null || true
        error START_FAILED "local trace lab identity is unavailable"
    }
    executable=$(readlink -- "/proc/$pid/exe") || {
        kill "$pid" 2>/dev/null || true
        error START_FAILED "local trace lab identity is unavailable"
    }
    temporary_state=$(mktemp "$state_dir/process.state.XXXXXX") || {
        kill "$pid" 2>/dev/null || true
        error STATE_INVALID "local process state cannot be created"
    }
    printf 'pid=%s\nport=%s\nstart_ticks=%s\nexecutable=%s\n' \
        "$pid" "$port" "$start_ticks" "$executable" > "$temporary_state"
    chmod 600 -- "$temporary_state"
    mv -- "$temporary_state" "$state_file"
    printf 'LAB_STARTED http://127.0.0.1:%s\n' "$port"
}

stop_lab() {
    if [ "$#" -ne 0 ]; then
        error ARGUMENT_INVALID "usage: scripts/lab.sh stop"
    fi
    ensure_state_dir
    if [ ! -e "$state_file" ] && [ ! -L "$state_file" ]; then
        error NOT_RUNNING "local trace lab is not running"
    fi
    read_state
    if ! process_matches; then
        if kill -0 "$saved_pid" 2>/dev/null; then
            error STATE_INVALID "refusing to signal an unrecognized process"
        fi
        rm -f -- "$state_file"
        error NOT_RUNNING "local trace lab is not running"
    fi
    kill -TERM "$saved_pid" || error STOP_FAILED "local trace lab could not be stopped"
    attempts=0
    while process_matches && [ "$attempts" -lt 100 ]; do
        sleep 0.05
        attempts=$((attempts + 1))
    done
    if process_matches; then
        error STOP_FAILED "local trace lab did not stop"
    fi
    rm -f -- "$state_file"
    printf '%s\n' 'LAB_STOPPED'
}

if [ "$#" -lt 1 ]; then
    error ARGUMENT_INVALID "usage: scripts/lab.sh start [port] | stop"
fi
command=$1
shift
case "$command" in
    start) start_lab "$@" ;;
    stop) stop_lab "$@" ;;
    *) error ARGUMENT_INVALID "usage: scripts/lab.sh start [port] | stop" ;;
esac
