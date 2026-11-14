#!/usr/bin/env bash
# 牛来量化 启动脚本（Linux / macOS）
#
#   scripts/start.sh            创建/更新虚拟环境，校验配置，启动网页 + 定时任务（只监听 127.0.0.1）
#   scripts/start.sh run        跑一次完整链路；其他参数原样传给 python -m qfpy
#   scripts/start.sh test       跑单元测试
#
# 环境变量：QF_CONFIG（配置文件路径）、QF_GUSHI_KEY、QF_DINGTALK_WEBHOOK、QF_DINGTALK_SECRET、PYTHON（默认 python3）
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "创建虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV"
fi
# requirements 有变化时才重新安装
stamp="$VENV/.req-stamp"
if [ ! -f "$stamp" ] || [ requirements.txt -nt "$stamp" ] || [ requirements-dev.txt -nt "$stamp" ]; then
  echo "安装依赖 ..."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements-dev.txt
  touch "$stamp"
fi

QF=("$VENV/bin/python" -m qfpy)
[ -n "${QF_CONFIG:-}" ] && QF+=(-c "$QF_CONFIG")

case "${1:-serve}" in
  test)
    exec "$VENV/bin/python" -m pytest -q tests
    ;;
  serve)
    shift || true
    "${QF[@]}" check-config
    port=$("$VENV/bin/python" - <<'EOF'
import os, tomllib
p = os.environ.get("QF_CONFIG", "config/pipeline.toml")
print(tomllib.load(open(p, "rb")).get("web", {}).get("port", 8081))
EOF
)
    echo "牛来量化 网页：http://127.0.0.1:${port}  （云端机器请在自己电脑上先执行 ssh -N -L ${port}:127.0.0.1:${port} 用户@主机）"
    exec "${QF[@]}" serve "$@"
    ;;
  *)
    exec "${QF[@]}" "$@"
    ;;
esac
