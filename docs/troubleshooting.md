# Troubleshooting

[Korean](troubleshooting.ko.md)

## Docker Compose

Validate config before starting services:

```bash
docker compose --env-file profiles/vllm-qwen3-14b-awq.env --profile vllm config
docker compose --env-file profiles/ollama-qwen3.env --profile ollama config
```

## GPU and NVIDIA Container Toolkit

Symptoms:

- vLLM container exits during startup.
- `nvidia-smi` fails inside the container.
- Docker reports no GPU devices.

Checks:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.9.1-base-ubuntu24.04 nvidia-smi
```

If the host reports `Xid 79`, `GPU has fallen off the bus`, or `No devices were found`, treat it as driver, PCIe, power, or hardware-level instability before tuning vLLM.

## vLLM Memory

If startup fails with CUDA OOM or instability:

1. Use `profiles/vllm-qwen3-14b-awq-safe.env`.
2. Lower `MAX_MODEL_LEN`.
3. Lower `GPU_MEMORY_UTILIZATION`.
4. Restart the vLLM profile.

## Open WebUI Port Conflict

If port `3000` is already used, change:

```text
OPEN_WEBUI_PORT=3001
```

## SearXNG Search

Check JSON output:

```bash
curl "http://localhost:8081/search?q=vllm&format=json"
```

If JSON is disabled, confirm `searxng/settings.yml` includes:

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

If `/v1/models` returns fallback data with `agent_gateway_warning`, the gateway is running but the configured upstream backend is not reachable.
