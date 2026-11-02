#!/usr/bin/env bash
# Hermes Agent 설치 및 local-llm-starter (128K) 자동 연동 스크립트
set -euo pipefail

BASE_URL="${HERMES_BASE_URL:-http://localhost:8000/v1}"
MODEL_NAME="${HERMES_MODEL_NAME:-qwen3.8-flash-next-iq3_s}"
CONTEXT_LENGTH="${HERMES_CONTEXT_LENGTH:-124000}"

echo "=========================================================="
echo " [local-llm-starter] Hermes Agent 설치 및 연동 시작"
echo " Target Endpoint: ${BASE_URL}"
echo " Target Model:    ${MODEL_NAME} (Context: ${CONTEXT_LENGTH})"
echo "=========================================================="

# 1. Hermes Agent 설치 확인 및 자동 설치
if ! command -v hermes >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/hermes" ]]; then
  echo "→ Hermes Agent가 감지되지 않아 공식 설치 스크립트를 실행합니다..."
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive
fi

# PATH 추가
export PATH="$HOME/.local/bin:$PATH"

if ! command -v hermes >/dev/null 2>&1; then
  echo "❌ Hermes Agent 설치 경로를 찾을 수 없습니다 ($HOME/.local/bin/hermes)." >&2
  exit 1
fi

echo "✓ Hermes 버전: $(hermes --version | head -n 1)"

# 2. 로컬 vLLM / Strata 128K 연동 설정
echo "→ local-llm-starter 환경으로 Hermes 설정을 적용합니다..."
hermes config set model.provider custom
hermes config set model.base_url "${BASE_URL}"
hermes config set model.default "${MODEL_NAME}"
hermes config set model.context_length "${CONTEXT_LENGTH}"

# 3. 사내 폐쇄망/보안을 위해 텔레메트리 비활성화
hermes config set telemetry.shared_metrics.enabled false 2>/dev/null || true
hermes config set telemetry.shared_metrics.send false 2>/dev/null || true

# 4. 연동 동작 테스트
echo "→ 로컬 LLM 통신 테스트 실행 중..."
if hermes chat -q "Say_OK_only" >/dev/null 2>&1; then
  echo "✅ Hermes Agent 설정 및 로컬 LLM 연동 테스트 성공!"
  echo "👉 터미널에서 'hermes'를 입력하여 에이전트를 시작하세요."
else
  echo "⚠️ 통신 테스트에 실패했습니다. local-llm-starter(vLLM/Strata) 컨테이너가 켜져 있는지 확인하세요."
fi

