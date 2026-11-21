#!/usr/bin/env bash
# Install the ariel-ner-warm systemd template units into the user manager.
#
# Units are templated: ariel-ner-warm@<contour>.{service,timer}.
# Contours match the MCP_MEMORY_DATA_DIR suffix used by each client:
#   hermes, cowagent, mimocode
# (data dirs: ~/.mcp-ariel-memory-<contour>)
#
# This script installs the units and reloads the user systemd manager.
# It does NOT enable any timer — enabling is the owner's decision (Lily).
#
# Usage:
#   scripts/install-ner-warm-timers.sh
#
# After running, inspect:
#   systemctl --user list-timers 'ariel-ner-warm*' --all
#   systemctl --user list-unit-files 'ariel-ner-warm*'

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Target repo root (the checkout whose venv + scripts the timer will run).
# Defaults to the parent of this script; pass an explicit path to target the
# canonical deployment checkout instead of the one you happen to run from.
#   scripts/install-ner-warm-timers.sh [/path/to/mcp-ariel-memory]
REPO_ROOT="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
WARM_SCRIPT="${REPO_ROOT}/scripts/warm_ner_cache.py"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "error: venv python not found at $VENV_PYTHON" >&2
    exit 1
fi
if [[ ! -f "$WARM_SCRIPT" ]]; then
    echo "error: warm_ner_cache.py not found at $WARM_SCRIPT" >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/ariel-ner-warm@.service" <<EOF
[Unit]
Description=Ariel NER cache warmer (%i)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
Environment=MCP_MEMORY_DATA_DIR=/home/murat/.mcp-ariel-memory-%i
ExecStart=$VENV_PYTHON $WARM_SCRIPT
TimeoutStartSec=900
EOF

cat > "$SYSTEMD_USER_DIR/ariel-ner-warm@.timer" <<EOF
[Unit]
Description=Nightly Ariel NER warm (%i)

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload

echo "installed template units to $SYSTEMD_USER_DIR"
echo
systemctl --user list-unit-files 'ariel-ner-warm*' --no-pager
echo
echo "timers are NOT enabled. Owner enables per-contour, e.g.:"
echo "  systemctl --user enable --now ariel-ner-warm@hermes.timer"
