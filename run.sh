#!/bin/bash
cd "$(dirname "$0")" || exit 1
PORT="${1:-8787}"
echo "启动早读语音评测系统，浏览器打开 http://127.0.0.1:${PORT}/"
exec python3 server.py "$PORT"
