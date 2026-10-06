#!/usr/bin/env bash
# Dump the Python stacks of a running Ariel memory server — no root, no debugger.
#
# Why a script exists for `kill -USR1`: the pid you find is often the WRONG one.
# Measured 2026-10-06 on this host:
#   - mimocode is launched by opencode as `sh -c '...; exec-less python3 ...'`,
#     so the process tree is sh (wrapper) -> python (server). `sh` does NOT
#     forward SIGUSR1 to its child. Signalling the wrapper kills the wrapper and
#     dumps nothing.
#   - hermes/cowagent have no wrapper (the server is a direct child of the
#     gateway / dsh), so there MainPID's child is the server itself.
# This resolves the pid by the server's own environment (MCP_MEMORY_DATA_DIR),
# which is the only thing that reliably identifies the python process.
#
# Usage:  scripts/dump_stacks.sh [hermes|mimocode|cowagent]   (default: all)
# Output: appended to <data_dir>/logs/stack-dump.txt
set -uo pipefail

targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
    targets=(hermes mimocode cowagent)
fi

found_any=0
for base in "${targets[@]}"; do
    want=".mcp-ariel-memory-${base}"
    pid=""

    # Identify by environment, never by the outermost pid of a process tree.
    for candidate in $(pgrep -f "mcp_server/server" 2>/dev/null || true); do
        [ -r "/proc/${candidate}/environ" ] || continue
        dir=$(tr '\0' '\n' < "/proc/${candidate}/environ" 2>/dev/null \
              | sed -n 's/^MCP_MEMORY_DATA_DIR=//p')
        [ "$(basename "${dir:-}")" = "$want" ] || continue
        # The server itself is the one running the venv python module.
        if tr '\0' ' ' < "/proc/${candidate}/cmdline" 2>/dev/null | grep -q "mcp_server"; then
            pid="$candidate"
            break
        fi
    done

    if [ -z "$pid" ]; then
        echo "  ${base}: сервер не найден (не запущен?)" >&2
        continue
    fi

    dump="${dir%/}/logs/stack-dump.txt"
    before=0
    [ -f "$dump" ] && before=$(wc -c < "$dump")

    if ! kill -USR1 "$pid" 2>/dev/null; then
        echo "  ${base}: kill -USR1 ${pid} не удался" >&2
        continue
    fi
    sleep 1

    after=0
    [ -f "$dump" ] && after=$(wc -c < "$dump")
    if [ "$after" -gt "$before" ]; then
        echo "  ${base}: дамп добавлен (pid=${pid}, +$((after - before)) байт) → ${dump}"
        found_any=1
    else
        echo "  ${base}: сигнал ушёл (pid=${pid}), но дамп не вырос — процесс без faulthandler?" >&2
    fi
done

if [ "$found_any" -eq 0 ]; then
    echo "Ни одного дампа не получено. Проверьте, что процессы памяти запущены и что в них новый код (mcp_server/server.py:_install_faulthandler)." >&2
    exit 1
fi

echo
echo "Читать стек конкретной службы (CEST, как в логах):"
echo "  grep -n -A40 \"pid=<pid>\" <data_dir>/logs/stack-dump.txt"
