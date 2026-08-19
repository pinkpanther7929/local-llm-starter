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

Run `scripts/gpu-watch.sh report` to read the `Xid` and the telemetry from the
hang. See [GPU Hang Evidence](operations.md#gpu-hang-evidence) to set up the
collector before it happens again.

### Xid 79 while the card is idle

Read the utilization column of the samples before the gap first, because it
decides whether any of the vLLM tuning below is even relevant.

On this host the card dropped off the bus at 0% utilization, 210 MHz and 21 W,
its lowest power state, with no CUDA kernel running and no AER or `pcieport`
errors beforehand. Nothing vLLM does can cause that. An idle `Xid 79` points at
the PCIe link, power-state transitions, or the card's power delivery, so change
one host-level thing at a time and let `gpu-watch` judge it:

1. Disable PCIe ASPM, the most common cause of an idle bus drop on consumer
   boards. Add `pcie_aspm=off pcie_port_pm=off` to the kernel command line, and
   disable ASPM in the BIOS too, since firmware can override the kernel.
2. Disable Resizable BAR in the BIOS.
3. Keep the card out of its deepest idle state: `nvidia-smi -pm 1` plus
   `nvidia-smi -lgc 510,2565`. This costs idle power and heat, so prefer it
   after the two BIOS-level changes.
4. Cap the PCIe link speed to Gen3 in the BIOS.
5. Reseat the card and its power cables, and check the 12VHPWR connector.

Mean time to failure here ranged from 42 minutes to 17 hours, so give each
change at least 48 hours before crediting it.

FlashInfer kernels have hung this card before, on both the sampler and the
attention path. Check which backend was selected:

```bash
docker logs vllm 2>&1 | grep -iE "attention.?backend"
```

The pattern has to match both log lines. Auto-selection logs `Using X attention
backend out of potential backends`, while an explicitly pinned backend takes a
different branch and logs `Using AttentionBackendEnum.X backend`. Matching only
the first makes a correctly pinned server look like it printed nothing.

If it picked `FLASHINFER`, pin `ATTENTION_BACKEND`, which is passed through as
vLLM's `--attention-backend` flag. The `VLLM_ATTENTION_BACKEND` environment
variable was removed in vLLM 0.23, so setting it does nothing.

Note that `KV_CACHE_DTYPE=fp8` rules `FLASH_ATTN` out of the candidate list, so
fp8 profiles have to use `TRITON_ATTN`.

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
