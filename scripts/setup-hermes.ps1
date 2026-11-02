# Hermes Agent Windows 설치 및 local-llm-starter 연동 스크립트
param (
    [string]$BaseUrl = "http://10.6.6.56:8000/v1",
    [string]$ModelName = "qwen3.8-flash-next-iq3_s",
    [int]$ContextLength = 124000
)

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [local-llm-starter] Hermes Agent Windows 설정 및 연동" -ForegroundColor Cyan
Write-Host " Target Endpoint: $BaseUrl"
Write-Host " Target Model:    $ModelName (Context: $ContextLength)"
Write-Host "=========================================================="

# 1. Hermes Agent 설치 확인
if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
    Write-Host "→ Hermes Agent가 감지되지 않아 Windows 공식 설치를 실행합니다..." -ForegroundColor Yellow
    Invoke-Expression (Invoke-RestMethod https://hermes-agent.nousresearch.com/install.ps1)
}

# 2. 로컬 LLM 연동 설정
Write-Host "→ local-llm-starter 환경으로 Hermes 설정을 적용합니다..." -ForegroundColor Green
hermes config set model.provider custom
hermes config set model.base_url $BaseUrl
hermes config set model.default $ModelName
hermes config set model.context_length $ContextLength

# 3. 텔레메트리 비활성화
hermes config set telemetry.shared_metrics.enabled false 2>$null
hermes config set telemetry.shared_metrics.send false 2>$null

# 4. 연동 동작 테스트
Write-Host "→ 로컬 LLM 통신 테스트 실행 중..." -ForegroundColor Green
try {
    $res = hermes -z "Say OK only."
    Write-Host "✅ Hermes Agent 설정 및 로컬 LLM 연동 성공!" -ForegroundColor Green
    Write-Host "👉 터미널에서 'hermes'를 입력하여 에이전트를 시작하세요." -ForegroundColor Cyan
} catch {
    Write-Host "⚠️ 통신 테스트 실패: local-llm 서버($BaseUrl)가 켜져 있는지 확인하세요." -ForegroundColor Red
}

