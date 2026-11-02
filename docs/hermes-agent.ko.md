# Hermes Agent 연동 가이드

[English](hermes-agent.md) | 한국어

Nous Research의 자율 AI 에이전트인 [Hermes Agent](https://hermes-agent.nousresearch.com/)를 `local-llm-starter`의 대용량 컨텍스트 모델(Strata Qwen3.8-Flash-Next 128K)에 직결하여 사용하는 방법입니다.

---

## 1. 개요

- **Hermes Agent**: 도구 호출(Tool Calling), 장기 기억(Memory), 스킬 자동 생성, 터미널 TUI를 지원하는 자율형 에이전트.
- **적합 모델**: `profiles/strata-qwen3.8-flash-next-128k.env` (124K~128K 컨텍스트).
  - Hermes Agent는 최소 64K 이상의 컨텍스트를 권장하며, Strata 128K 프로필 사용 시 원활한 다중 턴 및 툴 실행이 가능합니다.

---

## 2. 빠른 설정 (원클릭)

### Linux (로컬 머신 직접 실행)
```bash
# 기본값: http://localhost:8000/v1, qwen3.8-flash-next-iq3_s, context 124K
./scripts/setup-hermes.sh
```

### Windows (개발자 PC에서 원격 연동)
```powershell
# 기본값: http://10.6.6.56:8000/v1
.\scripts\setup-hermes.ps1
```

---

## 3. 수동 설정 방법

Hermes CLI가 이미 설치되어 있는 경우 아래 4줄의 명령어로 바로 연동 가능합니다.

```bash
# 1. Custom OpenAI 호환 엔드포인트 지정
hermes config set model.provider custom

# 2. Strata / vLLM 엔드포인트 URL 지정 (서버 로컬이면 localhost, 외부면 해당 IP)
hermes config set model.base_url http://localhost:8000/v1

# 3. 128K 서빙 모델명 지정
hermes config set model.default qwen3.8-flash-next-iq3_s

# 4. 컨텍스트 길이 지정 (124K~128K)
hermes config set model.context_length 124000

# 5. 사내 보안을 위해 외부 텔레메트리 끄기
hermes config set telemetry.shared_metrics.enabled false
hermes config set telemetry.shared_metrics.send false
```

---

## 4. 동작 테스트

```bash
hermes chat -q "Say OK only."
```

출력 결과로 `OK`가 나오면 정상 연동된 것입니다.

---

## 5. 실행 및 사용법

- **대화형 TUI 실행**: 터미널에서 `hermes` 입력.
- **단일 쿼리 실행**: `hermes chat -q "명령 내용"`
- **세션 이어하기**: `hermes -c` 또는 `hermes --resume latest`

---

## 6. 비상 중지 및 제어 (Emergency Stop)

에이전트가 루프에 빠지거나 너무 느려졌을 때 즉시 중지하는 방법입니다.

### 1) 실행 중 실시간 중단
- `Ctrl + C` 1회: 현재 도구 실행 및 추론 즉시 취소 (인터럽트).
- `Ctrl + C` 2회: Hermes 프로세스 완전 종료.

### 2) 비상 정지 명령어
```bash
# 모든 백그라운드 턴/스케줄러 즉시 일시정지
hermes pause

# 일시정지 해제 및 재개
hermes resume
```

### 3) 프로세스 강제 종료
- **Linux**: `pkill -9 -f hermes`
- **Windows**: `taskkill /F /IM hermes.exe`


