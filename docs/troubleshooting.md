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

Check the root port's own capabilities before spending days on a candidate:

```bash
sudo lspci -vv -s 00:01.0 | grep -iE "LnkCap:|LnkSta:"
sudo lspci -vv -s 01:00.0 | grep -iE "ASPM|LnkCtl:|LnkSta:"
```

Context length is not the origin, however tempting it is to blame the profile
that was in use when the hangs got noticed. The card dropping off the bus and
needing a reboot was recorded on 2026-08-06, eight days before the 64K profile
existed, while the default was 12288 with an `auto` KV cache. The only way the
64K profile could still matter is the resident allocation it implies, since it
raises utilization from 0.85 to 0.95, and that costs nothing to test by
reverting the profile.

On this host the root port reports `ASPM not supported`, which means ASPM was
never active on the GPU link and disabling it changes nothing. The root port
also reports `Width x8`, so the `x8 (downgraded)` warning on the card is CPU
lane bifurcation, not a bad contact, and reseating the card would prove nothing.

What the same output does show is that the link drops to 2.5 GT/s when idle and
retrains to 16 GT/s under load. Combined with the card dying at its lowest power
state, the low-power transitions themselves are the strongest candidate:

1. **Tested here and did not work.** Keep the card out of its deepest idle
   state, which suppresses both the P8 power state and the idle link downshift.
   The card still died 4h47m later at 44.7 W, 510 MHz and 0% utilization, so the
   lock was demonstrably in force and the idle power state was not the cause.
   Recorded because ruling it out costs a day and the lever is not obvious.
   The memory clock is the lever here.
   Locking the graphics clock alone raised the SM floor to 510 MHz and changed
   nothing else: the card stayed in P8 and the link stayed at 2.5 GT/s. Locking
   the memory clock moved it to P2 and held the link at 16 GT/s.

   Read the maximum first, since `-lmc` snaps to a supported step:

   ```bash
   nvidia-smi --query-gpu=clocks.max.mem --format=csv,noheader
   ```

   Install it as a unit, because a crash reboot would otherwise drop the lock
   and silently end the trial it was meant to run:

   ```ini
   # /etc/systemd/system/gpu-clocklock.service
   [Unit]
   Description=Keep the GPU out of its deepest idle power state
   After=multi-user.target

   [Service]
   Type=oneshot
   RemainAfterExit=yes
   ExecStart=/usr/bin/nvidia-smi -pm 1
   ExecStart=/usr/bin/nvidia-smi -lgc 510,2565
   ExecStart=/usr/bin/nvidia-smi -lmc 10501
   ExecStop=/usr/bin/nvidia-smi -rgc
   ExecStop=/usr/bin/nvidia-smi -rmc

   [Install]
   WantedBy=multi-user.target
   ```

   Verify all three, not just the service state:

   ```bash
   nvidia-smi --query-gpu=pstate,clocks.sm,clocks.mem,power.draw --format=csv,noheader
   sudo lspci -vv -s 00:01.0 | grep "LnkSta:"
   ```

   Idle draw rises from about 21 W to 44 W. That is the cost.
2. Disable PCIe power management: `pcie_aspm=off pcie_port_pm=off` on the kernel
   command line. Worth keeping since it also disables the PCI-PM L1 substates,
   but do not expect much where the root port has no ASPM to begin with.
3. Disable Resizable BAR in the BIOS.
4. Cap the PCIe link speed to Gen3 in the BIOS.
5. Reseat the card and its power cables, and check the 12VHPWR connector. Only
   worth doing if the root port and card widths actually disagree.

Mean time to failure here ranged from 42 minutes to 17 hours, so give each
change at least 48 hours before crediting it.

When the host-level candidates run out, stop the backend entirely and leave
`gpu-watch` running. Every hang so far happened at 0% utilization with vLLM
resident and holding 23.5 GiB, so bare idle separates the two remaining stories:
a card that dies with no CUDA context at all is a hardware fault and belongs in
an RMA, while one that survives days idle and empty points at the resident
allocation rather than at anything vLLM computes.

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

## SearXNG Searchㅇ

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
