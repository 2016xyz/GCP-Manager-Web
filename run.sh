#!/usr/bin/env bash
# GCP Manager Web · 开发/手动启动脚本
#
# 若 install.sh 已建好 .venv，这里会直接复用它；
# 否则退回到系统 python3 + pip 安装。
#
#   PORT=9000 ./run.sh
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

# 优先复用 install.sh 建的虚拟环境
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  PY="python3"
fi

if ! $PY -c "import fastapi, uvicorn, paramiko, PIL" 2>/dev/null; then
  echo "[*] 缺少依赖，安装中（网络不通会自动切清华镜像）…"
  MIRROR=()
  $PY -c "import socket;socket.setdefaulttimeout(4);socket.create_connection(('pypi.org',443)).close()" 2>/dev/null \
    || MIRROR=(-i https://pypi.tuna.tsinghua.edu.cn/simple)
  $PY -m pip install ${MIRROR[@]+"${MIRROR[@]}"} -r requirements.txt
fi

mkdir -p data/keys

echo ""
echo "==============================================="
echo "  GCP Manager Web"
echo "  控制台:  http://127.0.0.1:${PORT}/"
echo "  文档:    http://127.0.0.1:${PORT}/docs"
echo "  数据目录: $(pwd)/data"
echo "  解释器:   $PY"
if [ ! -f data/INITIAL_ADMIN.txt ] && [ ! -f data/gcp_web.db ]; then
  echo "  首次启动会生成随机管理员密码 → data/INITIAL_ADMIN.txt"
fi
echo "==============================================="
echo ""

exec $PY -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level warning
