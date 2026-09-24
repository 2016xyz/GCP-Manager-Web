#!/usr/bin/env bash
# GCP Manager Web 一键启动
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "[*] 安装依赖…"
  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
fi

if [ ! -f data/keys/.gitkeep ]; then mkdir -p data/keys && touch data/keys/.gitkeep; fi

echo ""
echo "==============================================="
echo "  GCP Manager Web"
echo "  控制台:  http://127.0.0.1:${PORT}/"
echo "  文档:    http://127.0.0.1:${PORT}/docs"
echo "  数据目录: $(pwd)/data"
echo "==============================================="
echo ""

exec python3 -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level warning
