# 운영

## 저장소 자동 검색 및 Perforce sync

Qwen3.8 56K 프로필은 `/opt/TS_BuildMachine-10.6.6.56_main`을 `/knowledge`에
읽기 전용 연결합니다. `저장소에서 CharacterMovement 구현 찾아줘`처럼 한글로
요청하면 검색 전에 호스트 서비스가 자동 sync합니다. 질문당 한 번만 실행하며,
sync 실패 시 파일 검색·읽기를 중단합니다. 일반 대화는 sync하지 않습니다.

**최초 설정 필요:** [영문 문서의 설치 명령](operations.md#automatic-perforce-refresh-before-local-search)을
따라 `/etc/local-llm-p4-sync.env`에 실제 P4 실행 파일·서버·사용자·클라이언트·티켓 경로를
입력하고 `local-llm-p4-sync` 서비스를 등록하세요. 비밀번호·티켓 본문은 Git에 넣지 마세요.
서비스는 현재 호스트 구성에 맞춰 root로 실행하며, 지정된 Unix 소켓만 사용합니다.

워크스페이스 Root 일치, `noclobber noallwrite`, 열린 수정 파일 없음이 필요합니다.
조건 불충족 시 설정을 임의로 바꾸지 않고 거부합니다. `sync -f`, revert, submit은
실행하지 않습니다. 안전 동기화(`p4 sync -s`)도 뷰에 따라 파일 갱신·삭제를 할 수 있으므로,
실제 빌드 작업과 공유 중이라면 전용 계정·워크스페이스를 권장합니다.

서비스 등록 후 56K env로 **agent-gateway만** 재빌드·재생성하면 됩니다.
`P4_SYNC_ENABLED=false`는 동기화만 끄고, `FILE_SEARCH_ENABLED=false`는 검색도 끕니다.
검색은 인덱스 기반 RAG가 아니라 파일 탐색이며, 기본 256KiB 초과 파일과 Unreal 생성
폴더(`Binaries`, `Intermediate`, `DerivedDataCache`, `Saved`)는 검색 대상에서 제외합니다.

[English](operations.md) | Korean

새 머신에서 repository를 clone한 뒤, 또는 profile과 port를 바꾼 뒤 이 스크립트로 상태를 확인합니다.

## Host 준비 확인

```bash
cp .env.example .env
scripts/check-host.sh
```

이 스크립트는 Docker, Docker Compose, NVIDIA GPU 표시 여부, 주요 port, disk 여유 공간, Compose 렌더링, file search가 켜진 경우 local file mount 경로를 확인합니다.

특정 env file을 사용하려면:

```bash
scripts/check-host.sh --env-file profiles/vllm-qwen3-14b-awq.env
```

## Runtime smoke test

먼저 backend profile 하나를 실행합니다:

```bash
docker compose --profile vllm up -d --build
```

그 다음 실행합니다:

```bash
scripts/smoke-test.sh
```

이 스크립트는 다음을 확인합니다:

- agent gateway `/health`
- agent gateway `/v1/models`
- backend `/v1/models`
- gateway를 통한 chat completion
- SearXNG JSON search
- Open WebUI HTTP 응답

원격 host를 확인하려면:

```bash
SMOKE_HOST=10.6.6.56 scripts/smoke-test.sh
```

모델이 아직 loading 중이면 chat completion을 건너뜁니다:

```bash
SMOKE_SKIP_CHAT=true scripts/smoke-test.sh
```

cold start 시간이 길면 timeout을 늘립니다:

```bash
SMOKE_CHAT_TIMEOUT=180 scripts/smoke-test.sh
```

## 큰 local folder mount

큰 source tree는 read-only mount를 유지하고 gateway가 host path를 보도록 설정합니다:

```env
FILE_SEARCH_ENABLED=true
FILE_SEARCH_HOST_PATH=/opt/my-source-tree
FILE_SEARCH_CONTAINER_PATH=/knowledge
```

gateway code나 file-search 환경만 바꾼 경우 gateway만 재시작합니다:

```bash
docker compose up -d --build agent-gateway
```

mount 확인:

```bash
docker exec local-llm-agent-gateway ls /knowledge
```

## GPU Hang 증거 수집

카드가 PCIe bus에서 떨어지면 `nvidia-smi`도 같이 죽기 때문에, hang 직전 상태는
미리 기록해 두지 않으면 사후에 확인할 방법이 없습니다.

kernel `Xid`가 reboot 후에도 남도록 persistent journal을 한 번 켭니다:

```bash
sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald
```

telemetry collector는 service로 띄웁니다:

```ini
# /etc/systemd/system/gpu-watch.service
[Unit]
Description=local-llm-starter GPU telemetry

[Service]
ExecStart=/opt/local-llm-starter/scripts/gpu-watch.sh watch
Environment=GPU_WATCH_LOG_DIR=/var/log/local-llm-starter
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gpu-watch
```

다음 hang 이후에는 한 번에 읽어옵니다:

```bash
GPU_WATCH_LOG_DIR=/var/log/local-llm-starter scripts/gpu-watch.sh report
```

이번 boot과 직전 boot의 kernel `Xid`, 카드가 사라지기 직전 telemetry sample,
vLLM container의 attention backend와 최근 error를 함께 출력합니다.
