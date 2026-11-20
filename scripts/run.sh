#!/usr/bin/env bash
set -euo pipefail
BRIOCHE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$BRIOCHE_ROOT/.venv/bin/python" ]]; then
  echo "请先按照 README 创建 .venv 并安装 Brioche。" >&2
  exit 1
fi
if [[ -d "$BRIOCHE_ROOT/runtime/linux/usr/lib/x86_64-linux-gnu" ]]; then
  export LD_LIBRARY_PATH="$BRIOCHE_ROOT/runtime/linux/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$BRIOCHE_ROOT/.venv/bin/python" -m brioche "$@"
