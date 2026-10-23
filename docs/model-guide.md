# Model Guide

[Korean](model-guide.ko.md)

Use model profiles to choose a backend and model without editing `docker-compose.yml`.

## vLLM Profiles

### `profiles/vllm-qwen3-14b-awq.env`

Default vLLM profile for a single RTX 4090 24GB class machine.

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq.env --profile vllm up -d --build
```

Settings:

- `MODEL_ID=Qwen/Qwen3-14B-AWQ`
- `SERVED_MODEL_NAME=qwen-14b`
- `MAX_MODEL_LEN=12288`
- `GPU_MEMORY_UTILIZATION=0.85`

### `profiles/vllm-qwen3-14b-awq-safe.env`

Conservative profile when stability and VRAM headroom matter more than context length.

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq-safe.env --profile vllm up -d --build
```

Settings:

- `MAX_MODEL_LEN=4096`
- `GPU_MEMORY_UTILIZATION=0.80`

### `profiles/vllm-qwen3.8-27b-awq-56k.env`

Text-only 56K context profile on a single RTX 4090 24GB without CPU weight offload.

```bash
docker compose --env-file profiles/vllm-qwen3.8-27b-awq-56k.env --profile vllm up -d --build
```

Settings:

- `MODEL_ID=cyankiwi/Qwen3.8-27B-AWQ-INT4`
- `SERVED_MODEL_NAME=qwen3.8-27b`
- `MAX_MODEL_LEN=57344`
- `KV_CACHE_DTYPE=fp8`
- `LOCAL_LANGUAGE_MODEL_ONLY=1`

## Strata Profiles

### `profiles/strata-qwen3.8-flash-next-128k.env`

Strata backend profile running Qwen3.8-Flash-Next (125B-A6B MoE) IQ3_S with 128K context, MTP, and GPU Vision on RTX 4090 24GB + 128GB RAM. Exposes the same OpenAI-compatible API (`/v1/models`, `/v1/chat/completions`, `/health`) on port `8000`.

```bash
docker compose --env-file profiles/strata-qwen3.8-flash-next-128k.env --profile strata up -d --build
```

Settings:

- `UPSTREAM_BASE_URL=http://strata:8000/v1`
- `SERVED_MODEL_NAME=qwen3.8-flash-next-iq3_s`
- `STRATA_MODEL=iq3_s`
- `STRATA_CONTEXT=128k` (131,072 tokens)
- `STRATA_VISION=gpu`
- `STRATA_ALLOWED_HOSTS`: allowlist for Docker internal DNS (`strata:8000`) rebinding protection

## Ollama Profiles

### `profiles/ollama-qwen3.env`

Ollama profile using the OpenAI-compatible `/v1` API.

```bash
docker compose --env-file profiles/ollama-qwen3.env --profile ollama up -d
docker exec -it ollama ollama pull qwen3:8b
```

Settings:

- `UPSTREAM_BASE_URL=http://ollama:11434/v1`
- `SERVED_MODEL_NAME=qwen3:8b`

## Environment Contract

- `UPSTREAM_BASE_URL`: OpenAI-compatible backend URL used by `agent-gateway`.
- `MODEL_ID`: backend model identifier used by vLLM.
- `SERVED_MODEL_NAME`: model name exposed to OpenWebUI and clients.
- `MAX_MODEL_LEN`: vLLM context length.
- `GPU_MEMORY_UTILIZATION`: vLLM GPU memory target.
- `HF_TOKEN`: Hugging Face token for gated or uncached models.

`agent-gateway` also accepts legacy `LLM_BASE_URL` and `VLLM_BASE_URL`, but new configs should use `UPSTREAM_BASE_URL`.
