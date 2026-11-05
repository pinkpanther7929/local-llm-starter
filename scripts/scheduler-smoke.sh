#!/usr/bin/env bash
# Smoke test for llm-scheduler: passthrough, priority header, status page.
# Usage: scripts/scheduler-smoke.sh [base_url=http://10.6.6.56:8000]
set -euo pipefail
BASE="${1:-http://10.6.6.56:8000}"
MODEL="${SERVED_MODEL_NAME:-qwen3.8-flash-next-iq3_s}"

echo "== models"; curl -fsS "$BASE/v1/models" | head -c 200; echo
echo "== chat (P0)"
curl -fsS "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -H 'X-LLM-Priority: P0' -H 'X-Caller: smoke' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":8,\"chat_template_kwargs\":{\"enable_thinking\":false}}" | head -c 400; echo
echo "== chat stream (P1)"
curl -fsSN "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -H 'X-LLM-Priority: P1' -H 'X-Caller: smoke' \
  -d "{\"model\":\"$MODEL\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":8,\"chat_template_kwargs\":{\"enable_thinking\":false}}" | tail -n 4
echo "== status"; curl -fsS "$BASE/v1/scheduler/status"; echo
