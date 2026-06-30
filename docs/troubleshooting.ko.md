# 문제 해결

[English](troubleshooting.md)

## Docker Compose

서비스 시작 전 config를 확인합니다:

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq.env --profile vllm config
docker compose --env-file profiles/ollama-qwen3.env --profile ollama config
```

## GPU와 NVIDIA Container Toolkit

증상:

- vLLM container가 startup 중 종료됩니다.
- container 안에서 `nvidia-smi`가 실패합니다.
- Docker가 GPU device를 찾지 못합니다.

확인:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.9.1-base-ubuntu24.04 nvidia-smi
```

Host에서 `Xid 79`, `GPU has fallen off the bus`, `No devices were found`가 보이면 vLLM 튜닝보다 driver, PCIe, power, hardware 레벨 문제를 먼저 봐야 합니다.

## vLLM Memory

CUDA OOM이나 startup instability가 있으면:

1. `profiles/vllm-qwen3-14b-awq-safe.env`를 사용합니다.
2. `MAX_MODEL_LEN`을 낮춥니다.
3. `GPU_MEMORY_UTILIZATION`을 낮춥니다.
4. vLLM profile을 다시 시작합니다.

## Open WebUI Port 충돌

`3000` port가 이미 사용 중이면 다음 값을 바꿉니다:

```text
OPEN_WEBUI_PORT=3001
```

## SearXNG Search

JSON output을 확인합니다:

```bash
curl "http://localhost:8081/search?q=vllm&format=json"
```

JSON이 꺼져 있으면 `searxng/settings.yml`에 아래 설정이 있는지 확인합니다:

```yaml
search:
  formats:
    - html
    - json
```

## Agent Gateway

Health check:

```bash
curl http://localhost:8010/health
curl http://localhost:8010/v1/models
```

`/v1/models`가 `agent_gateway_warning`과 함께 fallback data를 반환하면 gateway는 실행 중이지만 설정된 upstream backend에 연결하지 못한 상태입니다.
