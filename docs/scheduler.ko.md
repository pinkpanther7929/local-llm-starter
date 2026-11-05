# LLM 스케줄러

[English](scheduler.md) | 한국어

`llm-scheduler`는 Strata 백엔드 앞에서 생성 요청을 우선순위대로 줄 세웁니다. 여러 호출자(wiki-relay, 에이전트,
Jenkins, 다른 팀 배치 작업, Open WebUI)가 GPU 한 대를 나눠 써도 사람이 기다리는 요청이 밀리지 않게 하기 위한 것입니다.
요청과 응답 내용은 수정하지 않습니다.

```text
호출자 --> llm-scheduler --> strata (기본: 한 번에 1건)
Open WebUI --> agent-gateway --> llm-scheduler
```

## 동작

- `POST /v1/chat/completions`, `POST /v1/completions`만 우선순위 큐를 거칩니다. 나머지(`GET /v1/models` 등)는 바로 전달합니다.
- 요청/응답 본문은 그대로 전달하고, SSE 스트림은 도착하는 대로 중계합니다.
- 동시에 실행되는 생성은 최대 `SCHED_CONCURRENCY`건입니다(기본 `1`. Strata는 한 번에 하나씩 돌릴 때 가장 빠릅니다).
- `SCHED_AGING_SEC`보다 오래 기다린 요청은 간격마다 한 등급씩 올라가므로 P2도 굶지 않습니다.
- 대기 중 클라이언트가 끊으면 큐에서 제거되고, 업스트림 오류는 `502`로 응답하며 슬롯을 반환합니다.
- 이미 실행 중인 생성은 끊지 않습니다. 우선순위는 "다음 빈 슬롯을 누가 받는가"만 정합니다.

## 우선순위

선택 헤더 두 개만 추가하면 됩니다. 본문과 URL은 그대로입니다.

| 헤더 | 값 | 의미 |
| --- | --- | --- |
| `X-LLM-Priority` | `P0` | 사람이 기다리는 요청 (대화, 수동 재작성) |
| | `P1` | 파이프라인 요청 (커밋 초안, CI 실패 요약) |
| | `P2` | 배경 배치 (색인, 크롤링) |
| `X-Caller` | 짧은 이름 | 상태 화면 표시용. 순서에는 영향 없음 |

`X-LLM-Priority`가 없으면 `SCHED_IP_PRIORITY`의 IP별 기본값, 그다음 `SCHED_DEFAULT_PRIORITY`(`P2`)를 씁니다.
헤더는 보낸 값 그대로 신뢰합니다(사내 LAN 전제). `P0`를 남발하는 호출자는 `/v1/scheduler/status`에서 확인합니다.

```bash
curl http://10.6.6.56:8020/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-LLM-Priority: P0' -H 'X-Caller: my-service' \
  -d '{"model":"qwen3.8-flash-next-iq3_s","messages":[{"role":"user","content":"hi"}]}'
```

현재 호출자:

| 호출자 | 우선순위 |
| --- | --- |
| wiki-relay 수동 재작성 / 되돌리기 | P0 |
| wiki-relay 커밋 문서 초안 | P1 |
| wiki-relay 문서 카드 색인 | P2 |
| Open WebUI (agent-gateway 경유) | P0 (아래 전환 후) |

## 설정 (`.env`)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SCHED_PORT` | `8020` | 호스트 포트. 전환 후 `8000` |
| `SCHED_CONCURRENCY` | `1` | Strata로 동시에 보내는 생성 수 |
| `SCHED_AGING_SEC` | `120` | 한 등급 승격까지 대기 초. `0`이면 끔 |
| `SCHED_DEFAULT_PRIORITY` | `P2` | 헤더도 IP 매칭도 없을 때 등급 |
| `SCHED_IP_PRIORITY` | 비어 있음 | 소스 IP별 기본 등급. 예: `10.6.5.155=P2,10.6.6.10=P1` |
| `SCHED_UPSTREAM` | `http://strata:8000` | 백엔드 URL |
| `SCHED_UPSTREAM_TIMEOUT` | `900` | 업스트림 읽기 타임아웃(초) |

## 실행과 확인

```bash
docker compose --profile strata up -d --build llm-scheduler
scripts/scheduler-smoke.sh http://10.6.6.56:8020
curl http://10.6.6.56:8020/v1/scheduler/status
```

`/v1/scheduler/status`는 실행 중인 요청, 대기열(에이징 반영 등급 포함), 호출자별 누계(요청 수, 오류, 취소,
평균 대기/실행 시간, 출력 토큰, tok/s)를 반환합니다.

## 적용 절차

1. **시험(현재)**: 스케줄러 `:8020`, Strata는 `:8000`으로 직접 접근 가능한 상태. 호출자 하나(예: wiki-relay의
   `llm_endpoint`)를 `:8020`으로 돌려 검증합니다. `:8000`을 쓰는 호출자는 스케줄러를 거치지 않으므로 이 단계에서는
   그쪽과의 GPU 경합은 그대로입니다.
2. **전환**: `:8000`을 유일한 입구로 만듭니다.
   1. `10.6.6.56:8000`을 쓰는 다른 팀에 먼저 알립니다(보낼 헤더, 헤더 없는 요청은 `P2`로 처리됨).
   2. `docker-compose.yml`에서 `strata`의 `ports:`를 `expose: ["8000"]`로 바꿉니다.
   3. `.env`에 `SCHED_PORT=8000`을 설정합니다.
   4. `agent-gateway`의 `UPSTREAM_BASE_URL`을 `http://llm-scheduler:8000/v1`로 바꿉니다. 안 바꾸면 Open WebUI가
      도커 내부망으로 Strata에 바로 붙어 큐를 우회합니다. 게이트웨이의 upstream 요청에는
      `X-LLM-Priority: P0`, `X-Caller: open-webui`도 추가합니다.
   5. `docker compose --profile strata up -d`
3. **롤백**: `strata`의 `ports:`를 되돌리고 `SCHED_PORT=8020`, `UPSTREAM_BASE_URL`을 원래대로 둔 뒤
   `docker compose --profile strata up -d`. 스케줄러만 내리려면 `docker compose --profile strata rm -sf llm-scheduler`.

## 참고

- 스케줄러는 `strata` profile에 속합니다. `vllm`, `ollama` profile에서는 쓰지 않습니다.
- 상태 정보는 메모리에만 있어 재시작하면 초기화됩니다.
- 테스트: `python -m unittest scheduler.test_llm_scheduler`
