# 운영

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
