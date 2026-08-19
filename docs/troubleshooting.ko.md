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

hang 이후에는 `scripts/gpu-watch.sh report`로 `Xid`와 직전 telemetry를 확인합니다.
collector를 미리 걸어두는 방법은 [GPU Hang 증거 수집](operations.ko.md#gpu-hang-증거-수집)을 참고합니다.

이 카드에서는 FlashInfer kernel이 GPU를 매달아 놓은 전례가 있습니다. sampler와 attention 두 경로 모두 확인합니다:

```bash
docker logs vllm 2>&1 | grep -iE "attention.?backend"
```

두 형태의 로그를 모두 잡아야 합니다. 자동 선택은 `Using X attention backend out of potential backends`를 찍지만, backend를 명시하면 다른 분기를 타서 `Using AttentionBackendEnum.X backend`를 찍습니다. 앞쪽만 검색하면 제대로 고정된 서버가 아무것도 출력하지 않은 것처럼 보입니다.

`FLASHINFER`가 선택됐다면 `ATTENTION_BACKEND`로 고정합니다. 이 값은 vLLM의 `--attention-backend` flag로 전달됩니다. vLLM 0.23에서 `VLLM_ATTENTION_BACKEND` 환경변수는 제거됐으므로 환경변수로는 고정되지 않습니다.

`KV_CACHE_DTYPE=fp8`은 `FLASH_ATTN`을 후보에서 제외하므로 fp8 profile에서는 `TRITON_ATTN`을 씁니다.

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
