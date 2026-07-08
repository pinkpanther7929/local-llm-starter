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
    warn "${ENV_FILE} not found; using .env.example defaults for checks"
    set -a
    # shellcheck disable=SC1091
    . ".env.example"
    set +a
  else
    warn "no ${ENV_FILE} or .env.example found"
  fi
}

check_command() {
  if has_cmd "$1"; then
    ok "$1 is installed"
  else
    fail "$1 is not installed"
  fi
}

check_docker() {
  check_command docker
  if ! has_cmd docker; then
    return
  fi
  if docker info >/dev/null 2>&1; then
    ok "Docker daemon is reachable"
  else
    fail "Docker daemon is not reachable"
  fi
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin is available"
  else
    fail "docker compose plugin is not available"
  fi
  local compose_args=()
  if [[ -f "${ENV_FILE}" ]]; then
    compose_args=(--env-file "${ENV_FILE}")
  fi
  if docker compose "${compose_args[@]}" config >/dev/null 2>&1; then
    ok "docker-compose.yml renders successfully"
  else
    fail "docker compose config failed"
  fi
}

check_gpu() {
  if has_cmd nvidia-smi; then
    if nvidia-smi >/dev/null 2>&1; then
      ok "nvidia-smi can see NVIDIA GPU(s)"
      nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null |
        sed 's/^/[info] gpu: /'
    else
      fail "nvidia-smi is installed but cannot query GPU(s)"
    fi
  else
    warn "nvidia-smi not found; vLLM GPU checks are limited"
  fi

  if has_cmd docker && docker info >/dev/null 2>&1; then
    if docker info 2>/dev/null | grep -qi 'nvidia'; then
      ok "Docker reports NVIDIA runtime/support"
    else
      warn "Docker info does not mention NVIDIA runtime; verify NVIDIA Container Toolkit"
    fi
  fi
}

port_in_use() {
  local port="$1"
  if has_cmd ss; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
  elif has_cmd lsof; then
    lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 2
  fi
}

check_port() {
  local name="$1"
  local port="$2"
  if [[ -z "${port}" ]]; then
    warn "${name} port is empty"
    return
  fi
  if port_in_use "${port}"; then
    warn "${name} port ${port} is already in use"
  elif [[ "$?" -eq 2 ]]; then
    warn "cannot check ${name} port ${port}; ss/lsof not found"
  else
    ok "${name} port ${port} is available"
  fi
}

check_disk() {
  local path="${1:-.}"
  if [[ -e "${path}" ]]; then
    df -h "${path}" | awk 'NR==2 {printf "[info] disk: %s free on %s\n", $4, $6}'
    ok "disk check completed for ${path}"
  else
    warn "disk check path does not exist: ${path}"
  fi
}

check_file_search_path() {
  local enabled="${FILE_SEARCH_ENABLED:-false}"
  local host_path="${FILE_SEARCH_HOST_PATH:-./knowledge}"
  case "${enabled}" in
    1|true|TRUE|yes|YES)
      if [[ -d "${host_path}" ]]; then
        ok "FILE_SEARCH_HOST_PATH exists: ${host_path}"
      else
        fail "FILE_SEARCH_HOST_PATH does not exist: ${host_path}"
      fi
      ;;
    *)
      ok "local file search is disabled"
      ;;
  esac
}

main() {
  load_env
  check_docker
  check_gpu

  check_port "Open WebUI" "${OPEN_WEBUI_PORT:-3000}"
  check_port "agent gateway" "${AGENT_GATEWAY_PORT:-8010}"
  check_port "SearXNG" "${SEARXNG_PORT:-8081}"
  if [[ "${COMPOSE_PROFILES:-vllm}" == *"ollama"* ]]; then
    check_port "Ollama" "${OLLAMA_PORT:-11434}"
  else
    check_port "vLLM" "${VLLM_PORT:-8000}"
  fi

  check_disk "."
  check_disk "${HOME}/.cache/huggingface"
  check_file_search_path

  printf '\nsummary: %s ok, %s warn, %s fail\n' "${PASS_COUNT}" "${WARN_COUNT}" "${FAIL_COUNT}"
  if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    exit 1
  fi
}

main "$@"
