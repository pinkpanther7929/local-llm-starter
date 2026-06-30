# local-llm-starter

English | [Korean](README.ko.md)

Reusable starter kit for running a local LLM with web search in Open WebUI.
It runs Open WebUI, SearXNG, and an OpenAI-compatible agent gateway in front of either vLLM or Ollama.

## Features

- Open WebUI as the primary chat UI.
- SearXNG as the local web search backend.
- Agent gateway that exposes OpenAI-compatible `/v1/models` and `/v1/chat/completions`.
- Backend profiles for vLLM and Ollama.
- Environment-based ports, model names, and runtime settings.
- Model guide and troubleshooting docs for repeatable setup on new machines.

## Quickstart

### vLLM

```bash
cp .env.example .env
# Set HF_TOKEN if the model requires access.
docker compose --profile vllm up -d --build
```

Or use the validated model profile directly:

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq.env --profile vllm up -d --build
```

### Ollama

Edit `.env`:

```text
COMPOSE_PROFILES=ollama
UPSTREAM_BASE_URL=http://ollama:11434/v1
SERVED_MODEL_NAME=qwen3:8b
```

Then start:

```bash
docker compose --profile ollama up -d
docker exec -it ollama ollama pull qwen3:8b
```

Or use the Ollama profile directly:

```bash
docker compose --env-file profiles/ollama-qwen3.env --profile ollama up -d
docker exec -it ollama ollama pull qwen3:8b
```

## Endpoints

```text
Backend OpenAI-compatible API:
  vLLM: http://localhost:8000/v1
  Ollama: http://localhost:11434/v1
Open WebUI: http://localhost:3000
SearXNG: http://localhost:8081
Agent gateway OpenAI-compatible API: http://localhost:8010/v1
```

## Checks

```bash
curl http://localhost:8000/v1/models
curl http://localhost:8010/v1/models
curl "http://localhost:8081/search?q=vllm&format=json"
```

Agent gateway web answer check:

```bash
curl http://localhost:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-14b",
    "messages": [{"role": "user", "content": "Search the web for current vLLM tool calling support and summarize with URLs."}],
    "max_tokens": 512
  }'
```

## Validated vLLM Stack

```text
Base image: nvidia/cuda:12.9.1-devel-ubuntu24.04
Python: 3.12 virtualenv
PyTorch: CUDA 12.9 wheels
vLLM: 0.23.0 from wheels.vllm.ai/0.23.0/cu129
Model: Qwen/Qwen3-14B-AWQ
Served model name: qwen-14b
Max model length: 8192
```

## Notes

- Requires NVIDIA Container Toolkit and a GPU with enough VRAM for the selected model.
- Choose one backend profile per run. For vLLM, use `UPSTREAM_BASE_URL=http://vllm:8000/v1`; for Ollama, use `UPSTREAM_BASE_URL=http://ollama:11434/v1`.
- This setup was validated with `Qwen/Qwen3-14B-AWQ` after `google/gemma-4-12B-it` hit memory/version issues on a single RTX 4090 24GB.
- AWQ INT4 is more practical for 24GB VRAM than BF16/FP16 models.
- Open WebUI uses the agent gateway container DNS URL `http://agent-gateway:8010/v1`.
- Raw model calls without web tools can go directly to the backend endpoint.
- SearXNG JSON output is enabled in `searxng/settings.yml`; change `server.secret_key` before exposing SearXNG beyond localhost.
- `fetch_url` blocks private, loopback, link-local, multicast, and reserved addresses to reduce SSRF risk.
- If port `3000` conflicts with Grafana, set `OPEN_WEBUI_PORT=3001` in `.env`.

## Docs

- [Model Guide](docs/model-guide.md)
- [Troubleshooting](docs/troubleshooting.md)

## Jenkins Failure Analysis

Direct vLLM call:

```text
LOCAL_LLM_URL=http://10.6.6.56:8000/v1
LOCAL_LLM_MODEL=qwen-14b
```

Use the agent gateway when web tools are needed:

```text
LOCAL_LLM_URL=http://10.6.6.56:8010/v1
LOCAL_LLM_MODEL=qwen-14b
```

## License

MIT
