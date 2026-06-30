# local-llm-starter

Reusable local LLM starter kit for Open WebUI with web-enabled local models.
It runs Open WebUI, SearXNG, and an OpenAI-compatible agent gateway in front of a local backend.

Supported backend profiles:

- `vllm`: local OpenAI-compatible vLLM server.
- `ollama`: Ollama server with its OpenAI-compatible `/v1` API.

Current validated stack:

```text
Base image: nvidia/cuda:12.9.1-devel-ubuntu24.04
Python: 3.12 virtualenv
PyTorch: CUDA 12.9 wheels
vLLM: 0.23.0 from wheels.vllm.ai/0.23.0/cu129
Model: Qwen/Qwen3-14B-AWQ
Served model name: qwen-14b
Max model length: 16384
```

## Start

### vLLM

```bash
cp .env.example .env
# edit HF_TOKEN if the model requires access
docker compose --profile vllm up -d --build
```

### Ollama

Edit `.env`:

```text
COMPOSE_PROFILES=ollama
LLM_BASE_URL=http://ollama:11434/v1
SERVED_MODEL_NAME=llama3.1:8b
```

Then start:

```bash
docker compose --profile ollama up -d
docker exec -it ollama ollama pull llama3.1:8b
```

Endpoints:

```text
Backend OpenAI-compatible API:
  vLLM: http://localhost:8000/v1
  Ollama: http://localhost:11434/v1
Open WebUI: http://localhost:3000
SearXNG: http://localhost:8081
Agent gateway OpenAI-compatible API: http://localhost:8010/v1
```

Model list check:

```bash
curl http://localhost:8000/v1/models
```

Agent gateway model list check:

```bash
curl http://localhost:8010/v1/models
```

Web search check:

```bash
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

Example:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-14b",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 128
  }'
```

## Notes

- Requires NVIDIA Container Toolkit and a GPU with enough VRAM for the selected model.
- Choose one backend profile per run. For vLLM, keep `LLM_BASE_URL=http://vllm:8000/v1`; for Ollama, set `LLM_BASE_URL=http://ollama:11434/v1`.
- This setup was validated after `google/gemma-4-12B-it` hit memory/version issues on a single RTX 4090 24GB.
- The working model is AWQ INT4, which is more practical for 24GB VRAM than BF16/FP16 Gemma4 under vLLM.
- `Qwen/Qwen3-14B-AWQ` is currently preferred over the earlier 27B AWQ test because it supports a larger practical context window on the RTX 4090 setup.
- `runtime: nvidia`, `ipc: host`, and `shm_size: "16gb"` are part of the validated runtime shape.
- Open WebUI uses the agent gateway container DNS URL `http://agent-gateway:8010/v1`.
- Direct backend access remains available for raw model calls without web tools.
- Open WebUI web search uses container DNS URL `http://searxng:8080/search?q=<query>&format=json`.
- SearXNG JSON output is enabled in `searxng/settings.yml`; change `server.secret_key` before exposing SearXNG beyond localhost.
- Agent gateway listens on port `8010`, forwards OpenAI-compatible chat requests to the configured backend, and executes `search_web` / `fetch_url` tool calls on behalf of the model.
- `fetch_url` blocks private, loopback, link-local, multicast, and reserved addresses to reduce SSRF risk.
- Port `3000` conflicts with Grafana if the monitoring compose stack runs on the same host; change Open WebUI to `3001:8080` in that case.

## Jenkins Failure Analysis

Jenkins failure analysis uses:

```text
LOCAL_LLM_URL=http://10.6.6.56:8000/v1
LOCAL_LLM_MODEL=qwen-14b
```

`jenkins_failure_picker.py` sends OpenAI-compatible `/chat/completions` requests, disables Qwen thinking with `chat_template_kwargs.enable_thinking=false`, and falls back to heuristic suspect selection if the LLM is unavailable.

To allow Jenkins failure analysis to use web tools through the agent gateway, point it at:

```text
LOCAL_LLM_URL=http://10.6.6.56:8010/v1
LOCAL_LLM_MODEL=qwen-14b
```
