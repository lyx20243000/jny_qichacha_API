#!/bin/bash

set -e
# 自动获取 git commit hash 作为版本标识
export GIT_COMMIT_SHORT=$(git rev-parse --short HEAD 2>/dev/null || echo "dev")
# 导出环境变量

WORK_DIR="${COZE_WORKSPACE_PATH:-.}"
PORT="${DEPLOY_RUN_PORT:-5000}"

usage() {
  echo "用法: $0 -p <端口>"
}

while getopts "p:h" opt; do
  case "$opt" in
    p)
      PORT="$OPTARG"
      ;;
    h)
      usage
      exit 0
      ;;
    \?)
      echo "无效选项: -$OPTARG"
      usage
      exit 1
      ;;
  esac
done

# 激活 .venv（devbox 环境），deploy 无 .venv 则跳过
if [ -f "${WORK_DIR}/.venv/bin/activate" ]; then
  source "${WORK_DIR}/.venv/bin/activate"
fi

python ${WORK_DIR}/src/main.py -m http -p $PORT
