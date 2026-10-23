# 모델 가이드

[English](model-guide.md)

`docker-compose.yml`을 직접 수정하지 않고 model profile로 backend와 모델을 선택합니다.

## vLLM Profile

### `profiles/vllm-qwen3-14b-awq.env`

단일 RTX 4090 24GB급 머신용 기본 vLLM profile입니다.

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq.env --profile vllm up -d --build
```

설정:

- `MODEL_ID=Qwen/Qwen3-14B-AWQ`
- `SERVED_MODEL_NAME=qwen-14b`
- `MAX_MODEL_LEN=12288`
- `GPU_MEMORY_UTILIZATION=0.85`

### `profiles/vllm-qwen3-14b-awq-safe.env`

context 길이보다 안정성과 VRAM 여유가 더 중요할 때 쓰는 보수적 profile입니다.

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq-safe.env --profile vllm up -d --build
```

설정:

- `MAX_MODEL_LEN=4096`
- `GPU_MEMORY_UTILIZATION=0.80`

### `profiles/vllm-qwen3.8-27b-awq-56k.env`

단일 RTX 4090 24GB에서 CPU offload 없이 텍스트 전용 56K 컨텍스트를 운영하는 profile입니다.

```bash
docker compose --env-file profiles/vllm-qwen3.8-27b-awq-56k.env --profile vllm up -d --build
```

설정:

- `MODEL_ID=cyankiwi/Qwen3.8-27B-AWQ-INT4`
- `SERVED_MODEL_NAME=qwen3.8-27b`
- `MAX_MODEL_LEN=57344`
- `KV_CACHE_DTYPE=fp8`
- `LOCAL_LANGUAGE_MODEL_ONLY=1`

## Strata Profile

### `profiles/strata-qwen3.8-flash-next-128k.env`

RTX 4090 24GB + 128GB RAM 환경에서 Qwen3.8-Flash-Next (125B-A6B MoE) IQ3_S 128K 컨텍스트, MTP, GPU Vision을 구동하는 Strata 백엔드 profile입니다. 기존 vLLM과 동일한 OpenAI 호환 API(`/v1/models`, `/v1/chat/completions`, `/health`) 및 포트(`8000`)를 제공합니다.

```bash
docker compose --env-file profiles/strata-qwen3.8-flash-next-128k.env --profile strata up -d --build
```

설정:

- `UPSTREAM_BASE_URL=http://strata:8000/v1`
- `SERVED_MODEL_NAME=qwen3.8-flash-next-iq3_s`
- `STRATA_MODEL=iq3_s`
- `STRATA_CONTEXT=128k` (131,072 tokens)
- `STRATA_VISION=gpu`
- `STRATA_ALLOWED_HOSTS`: Docker 내부 DNS(`strata:8000`) rebinding 보호 허용 목록

## Ollama Profile

### `profiles/ollama-qwen3.env`

Ollama의 OpenAI 호환 `/v1` API를 사용하는 profile입니다.

```bash
docker compose --env-file profiles/ollama-qwen3.env --profile ollama up -d
docker exec -it ollama ollama pull qwen3:8b
```

설정:

- `UPSTREAM_BASE_URL=http://ollama:11434/v1`
- `SERVED_MODEL_NAME=qwen3:8b`

## Env 계약

- `UPSTREAM_BASE_URL`: `agent-gateway`가 호출하는 OpenAI 호환 backend URL.
- `MODEL_ID`: vLLM이 로드할 backend model identifier.
- `SERVED_MODEL_NAME`: OpenWebUI와 client에 노출할 model name.
- `MAX_MODEL_LEN`: vLLM context length.
- `GPU_MEMORY_UTILIZATION`: vLLM GPU memory target.
- `HF_TOKEN`: gated 또는 local cache가 없는 Hugging Face 모델용 token.

`agent-gateway`는 legacy `LLM_BASE_URL`, `VLLM_BASE_URL`도 받지만 새 설정은 `UPSTREAM_BASE_URL`을 사용합니다.
