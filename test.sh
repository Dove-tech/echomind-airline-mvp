#!/usr/bin/env bash

# 任何命令失败时立即停止脚本。
set -euo pipefail

# 可以通过环境变量覆盖接口地址。
API_BASE_URL="${AIRLINE_MVP_API_BASE_URL:-http://127.0.0.1:8000}"

echo "===== 1. 检查服务状态 ====="

curl \
  --silent \
  --show-error \
  --fail-with-body \
  "${API_BASE_URL}/health"

printf "\n\n"

echo "===== 2. 发送简单航班查询 ====="

curl \
  --silent \
  --show-error \
  --fail-with-body \
  --request POST \
  --url "${API_BASE_URL}/v1/chat" \
  --header "Content-Type: application/json; charset=utf-8" \
  --data-binary @- <<'JSON'
{
  "message": "请查询 CZ8888 航班 2026-07-29 的状态",
  "verified_subject_id": "subject_demo",
  "locale": "zh-CN"
}
JSON

printf "\n"