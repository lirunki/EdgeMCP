#!/usr/bin/env bash
# WSL wrapper for the Windows launcher. Resolve Windows paths/profile there,
# rather than hardcoding an account name or placing a profile on the WSL filesystem.
set -euo pipefail

for tool in powershell.exe wslpath; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "This launcher requires WSL with Windows interoperability ($tool missing)." >&2
    exit 1
  fi
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec powershell.exe -NoProfile -File "$(wslpath -w "$SCRIPT_DIR/launch-edge.ps1")" \
  -Port "${CDP_PORT:-9222}" "$@"
