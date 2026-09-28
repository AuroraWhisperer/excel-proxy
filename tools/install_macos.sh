#!/usr/bin/env bash
set -euo pipefail

if [[ "${OSTYPE:-}" != darwin* ]] && [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is for macOS only." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

need_cmd() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Missing required command: $name" >&2
    exit 1
  fi
}

need_cmd python3

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "Missing requirements file: ${REQ_FILE}" >&2
  exit 1
fi

echo "Creating virtual environment at ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

echo "Installing Python dependencies"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${REQ_FILE}"

mkdir -p "${HOME}/Library/Application Support/ghcp_proxy"
mkdir -p "${HOME}/Library/Caches/ghcp_proxy"
mkdir -p "${HOME}/.codex"

cat <<EOF

macOS setup complete.

Next steps:
  1. Activate the virtualenv:
     source "${VENV_DIR}/bin/activate"
  2. Start the service:
     python "${SCRIPT_DIR}/app/proxy.py"
  3. Open the dashboard:
     http://127.0.0.1:8000/

Notes:
  - Excel authentication loads automatically from the signed-in Excel WebKit session.
    If its token expires, refresh the official Excel task pane.
  - Codex activation is handled from the dashboard so backups stay intact.
  - The dashboard can install a macOS login item and zsh commands: start-ghproxy / stop-ghproxy.
EOF
