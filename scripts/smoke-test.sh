#!/usr/bin/env bash
set -u

ENV_FILE="${ENV_FILE:-.env}"
if [[ "${1:-}" == "--env-file" ]]; then
  ENV_FILE="${2:-}"
fi
if [[ -z "${ENV_FILE}" ]]; then
  echo "usage: $0 [--env-file .env]" >&2
  exit 2
fi

HOST="${SMOKE_HOST:-localhost}"
# agent-gateway and SearXNG are not published on the host; they are probed from
# inside the gateway container, which sits on the same Docker network.
GATEWAY_CONTAINER="${GATEWAY_CONTAINER:-local-llm-agent-gateway}"
TIMEOUT="${SMOKE_TIMEOUT:-20}"
CHAT_TIMEOUT="${SMOKE_CHAT_TIMEOUT:-90}"
CHAT_PROMPT="${SMOKE_CHAT_PROMPT:-Reply with exactly: local-llm-ok}"

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

ok() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf '[ok] %s\n' "$1"
}

warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  printf '[warn] %s\n' "$1"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf '[fail] %s\n' "$1"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
    ok "loaded ${ENV_FILE}"
  elif [[ -f ".env.example" ]]; then
    warn "${ENV_FILE} not found; using .env.example defaults"
    set -a
    # shellcheck disable=SC1091
    . ".env.example"
    set +a
  else
    warn "no ${ENV_FILE} or .env.example found"
  fi
}

http_get() {
  local url="$1"
  curl -fsS --max-time "${TIMEOUT}" "${url}"
}

internal_get() {
  docker exec "${GATEWAY_CONTAINER}" python -c \
    'import sys, urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=int(sys.argv[2])).read().decode("utf-8", "replace"))' \
    "$1" "${TIMEOUT}"
}

internal_post_json() {
  local url="$1"
  local data="$2"
  printf '%s' "${data}" | docker exec -i "${GATEWAY_CONTAINER}" python -c \
    'import sys, urllib.request; req = urllib.request.Request(sys.argv[1], data=sys.stdin.buffer.read(), headers={"Content-Type": "application/json"}); sys.stdout.write(urllib.request.urlopen(req, timeout=int(sys.argv[2])).read().decode("utf-8", "replace"))' \
    "${url}" "${CHAT_TIMEOUT}"
}

http_post_json() {
  local url="$1"
  local data="$2"
  curl -fsS --max-time "${CHAT_TIMEOUT}" \
    -H "Content-Type: application/json" \
    -d "${data}" \
    "${url}"
}

check_json_field() {
  local label="$1"
  local text="$2"
  local pattern="$3"
  if printf '%s' "${text}" | grep -qE "${pattern}"; then
    ok "${label}"
  else
    fail "${label}"
    printf '%s\n' "${text}" | head -20 | sed 's/^/[debug] /'
  fi
}

json_payload() {
  if has_cmd python3; then
    MODEL="${SERVED_MODEL_NAME:-qwen-14b}" PROMPT="${CHAT_PROMPT}" python3 - <<'PY'
import json
import os

print(json.dumps({
    "model": os.environ["MODEL"],
    "messages": [{"role": "user", "content": os.environ["PROMPT"]}],
    "max_tokens": 64,
}, ensure_ascii=False))
PY
  else
    printf '{"model":"%s","messages":[{"role":"user","content":"%s"}],"max_tokens":64}' \
      "${SERVED_MODEL_NAME:-qwen-14b}" "${CHAT_PROMPT}"
  fi
}

check_gateway() {
  local base="http://127.0.0.1:8010"
  local response

  if response="$(internal_get "${base}/health" 2>&1)"; then
    check_json_field "agent gateway /health" "${response}" '"ok"'
  else
    fail "agent gateway /health"
    printf '%s\n' "${response}" | sed 's/^/[debug] /'
    return
  fi

  if response="$(internal_get "${base}/v1/models" 2>&1)"; then
    check_json_field "agent gateway /v1/models" "${response}" '"data"'
  else
    fail "agent gateway /v1/models"
    printf '%s\n' "${response}" | sed 's/^/[debug] /'
  fi

  if [[ "${SMOKE_SKIP_CHAT:-false}" == "true" ]]; then
    warn "chat completion check skipped"
    return
  fi

  local payload
  payload="$(json_payload)"
  if response="$(internal_post_json "${base}/v1/chat/completions" "${payload}" 2>&1)"; then
    # Non-empty content, not just the presence of "choices". A GPU that has
    # fallen off the bus still returns a well-formed completion with an empty
    # content, which passed this check for three hours on 2026-08-24. This is
    # also the only check here that makes the GPU do work; /v1/models answers
    # from the API server and stays 200 with a dead card.
    check_json_field "agent gateway chat completion" "${response}" '"content": *"[^"]'
  else
    fail "agent gateway chat completion"
    printf '%s\n' "${response}" | head -40 | sed 's/^/[debug] /'
  fi
}

check_backend() {
  # The backend is not published; the gateway relays /v1/models through the scheduler.
  local port="${API_PORT:-8000}"

  local response
  if response="$(http_get "http://${HOST}:${port}/v1/models" 2>&1)"; then
    check_json_field "gateway -> scheduler -> backend /v1/models on port ${port}" "${response}" '"data"'
  else
    fail "gateway -> scheduler -> backend /v1/models on port ${port}"
    printf '%s\n' "${response}" | sed 's/^/[debug] /'
  fi
}

check_tool_service() {
  local response
  if response="$(internal_get "http://tool-service:8020/health" 2>&1)"; then
    check_json_field "tool-service /health" "${response}" '"ok"'
  else
    fail "tool-service /health"
    printf '%s\n' "${response}" | sed 's/^/[debug] /'
  fi
}

check_searxng() {
  local response
  if response="$(internal_get "http://searxng:8080/search?q=vllm&format=json" 2>&1)"; then
    check_json_field "SearXNG JSON search" "${response}" '"results"'
  else
    fail "SearXNG JSON search"
    printf '%s\n' "${response}" | sed 's/^/[debug] /'
  fi
}

check_open_webui() {
  local port="${OPEN_WEBUI_PORT:-3000}"
  local response
  if response="$(http_get "http://${HOST}:${port}/" 2>&1)"; then
    ok "Open WebUI responds on port ${port}"
  else
    warn "Open WebUI did not respond on port ${port}"
    printf '%s\n' "${response}" | sed 's/^/[debug] /'
  fi
}

main() {
  if ! has_cmd curl; then
    fail "curl is not installed"
    exit 1
  fi

  if ! has_cmd docker; then
    fail "docker is not installed (agent-gateway and SearXNG are probed through it)"
    exit 1
  fi

  load_env
  check_gateway
  check_backend
  check_tool_service
  check_searxng
  check_open_webui

  printf '\nsummary: %s ok, %s warn, %s fail\n' "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"
  if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    exit 1
  fi
}

main "$@"
