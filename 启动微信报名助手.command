#!/bin/zsh
set -u
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  osascript -e 'display dialog "未找到 Python 3。请先安装 Python 3，再双击此启动器。" buttons {"好"} default button 1'
  exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$APP_DIR/.venv" || exit 1
fi

if ! "$APP_DIR/.venv/bin/python" -c 'import pyautogui, rapidocr, onnxruntime, Quartz, AppKit' >/dev/null 2>&1; then
  osascript -e 'display notification "首次启动，正在安装组件，请稍候…" with title "微信活动报名助手"'
  "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
  "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
  if [[ $? -ne 0 ]]; then
    osascript -e 'display dialog "组件安装失败。请检查网络后重新双击启动器。" buttons {"好"} default button 1'
    exit 1
  fi
fi

exec "$APP_DIR/.venv/bin/python" "$APP_DIR/wechat_signup_app.py"
