# local-llm-starter

[English](README.md) | Korean

Open WebUI에서 웹 검색이 가능한 로컬 LLM을 빠르게 실행하기 위한 starter kit입니다.
vLLM 또는 Ollama backend 앞에 Open WebUI, SearXNG, OpenAI 호환 agent gateway를 구성합니다.

## 기능

- Open WebUI를 기본 채팅 UI로 사용합니다.
- SearXNG를 로컬 웹 검색 backend로 사용합니다.
- Agent gateway가 OpenAI 호환 `/v1/models`, `/v1/chat/completions`를 제공합니다.
- vLLM과 Ollama backend profile을 지원합니다.
- 포트, 모델 이름, runtime 설정을 `.env`로 관리합니다.
- 새 머신에서 반복 가능한 설정을 위해 model guide와 troubleshooting 문서를 제공합니다.

## 빠른 시작

### vLLM

```bash
cp .env.example .env
# 모델 접근 권한이 필요하면 HF_TOKEN을 설정합니다.
docker compose --profile vllm up -d --build
```

검증된 model profile을 바로 사용할 수도 있습니다:

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq.env --profile vllm up -d --build
```

### Ollama

`.env`를 수정합니다:

```text
COMPOSE_PROFILES=ollama
UPSTREAM_BASE_URL=http://ollama:11434/v1
SERVED_MODEL_NAME=qwen3:8b
```

실행합니다:

```bash
docker compose --profile ollama up -d
docker exec -it ollama ollama pull qwen3:8b
```

Ollama profile을 바로 사용할 수도 있습니다:

```bash
docker compose --env-file profiles/ollama-qwen3.env --profile ollama up -d
docker exec -it ollama ollama pull qwen3:8b
```

## Endpoint

```text
Backend OpenAI-compatible API:
  vLLM: http://localhost:8000/v1
  Ollama: http://localhost:11434/v1
Open WebUI: http://localhost:3000
SearXNG: http://localhost:8081
Agent gateway OpenAI-compatible API: http://localhost:8010/v1
```

## 확인

```bash
curl http://localhost:8000/v1/models
curl http://localhost:8010/v1/models
curl "http://localhost:8081/search?q=vllm&format=json"
```

Agent gateway 웹 검색 답변 확인:

```bash
curl http://localhost:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen-14b",
    "messages": [{"role": "user", "content": "Search the web for current vLLM tool calling support and summarize with URLs."}],
    "max_tokens": 512
  }'
```

## 검증된 vLLM Stack

```text
Base image: nvidia/cuda:12.9.1-devel-ubuntu24.04
Python: 3.12 virtualenv
PyTorch: CUDA 12.9 wheels
vLLM: 0.23.0 from wheels.vllm.ai/0.23.0/cu129
Model: Qwen/Qwen3-14B-AWQ
Served model name: qwen-14b
Max model length: 12288
```

## 메모

- NVIDIA Container Toolkit과 선택한 모델을 실행할 수 있는 GPU VRAM이 필요합니다.
- 한 번에 backend profile 하나만 선택합니다. vLLM은 `UPSTREAM_BASE_URL=http://vllm:8000/v1`, Ollama는 `UPSTREAM_BASE_URL=http://ollama:11434/v1`을 사용합니다.
- 이 설정은 단일 RTX 4090 24GB에서 `google/gemma-4-12B-it`가 메모리/버전 문제를 낸 뒤 `Qwen/Qwen3-14B-AWQ`로 검증했습니다.
- AWQ INT4 모델은 24GB VRAM에서 BF16/FP16 모델보다 현실적입니다.
- Open WebUI는 agent gateway의 container DNS URL인 `http://agent-gateway:8010/v1`을 사용합니다.
- Web tool 없는 raw model 호출은 backend endpoint로 직접 보낼 수 있습니다.
- SearXNG JSON output은 `searxng/settings.yml`에서 켜져 있습니다. 외부에 노출하기 전 `server.secret_key`를 바꾸세요.
- `fetch_url`은 SSRF 위험을 줄이기 위해 private, loopback, link-local, multicast, reserved address를 차단합니다.
- `3000` port가 Grafana와 충돌하면 `.env`에서 `OPEN_WEBUI_PORT=3001`처럼 바꾸세요.

## 문서

- [모델 가이드](docs/model-guide.ko.md)
- [문제 해결](docs/troubleshooting.ko.md)

## Jenkins Failure Analysis

vLLM 직접 호출:

```text
LOCAL_LLM_URL=http://10.6.6.56:8000/v1
LOCAL_LLM_MODEL=qwen-14b
```

웹 tool이 필요하면 agent gateway를 사용합니다:

```text
LOCAL_LLM_URL=http://10.6.6.56:8010/v1
LOCAL_LLM_MODEL=qwen-14b
```

## License

MIT
