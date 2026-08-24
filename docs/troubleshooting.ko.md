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

죽은 카드는 호출자에게 티를 내지 않습니다. 2026-08-24에 `Xid 79`가 10:11:47에 찍힌 뒤 실제로 요청이 멈춘 13:35까지 3시간 넘게, vLLM은 `/v1/chat/completions`에 HTTP 200과 **빈 `content`** — completion token 1개, `finish_reason: "stop"` — 를 돌려줬습니다. `/v1/models`는 API server가 처리하고 GPU를 건드리지 않으므로 그 시간 내내 200이었습니다. 카드가 살아있음을 증명하는 것은 chat completion뿐이고, 그것도 content가 비어있지 않을 때만입니다. 이제 gateway는 빈 completion에 502를 반환하고 `smoke-test.sh`는 fail 처리합니다.

### 유휴 상태에서 발생한 Xid 79

먼저 gap 직전 sample의 utilization 열을 봅니다. 아래의 vLLM 튜닝이 애초에 관련 있는지를 이 열이 결정합니다.

이 host에서는 카드가 0% utilization, 210MHz, 21W — 최저 전력 상태에서 버스에서 떨어졌습니다. CUDA kernel이 돌지 않았고, 선행하는 AER이나 `pcieport` 오류도 없었습니다. vLLM이 하는 어떤 일도 이런 고장을 만들 수 없습니다. 유휴 상태의 `Xid 79`는 PCIe link, 전력 상태 전환, 카드 전원 공급 쪽을 가리킵니다. host 레벨 변경을 **한 번에 하나씩** 적용하고 `gpu-watch`로 판정합니다.

이후 카드는 유휴 전력 범위의 양쪽 끝에서 모두 죽었습니다. 2026-08-19에는 최저 상태인 21W / 210MHz에서, 2026-08-24에는 clock lock이 걸린 44.45W / 510MHz / `P2`에서 죽었고 마지막 sample은 `Xid` 15초 전이었습니다. 두 번 다 utilization은 0%였습니다. 유휴 전력 상태는 살아남는 run과 죽는 run을 가르지 못합니다. 아래 후보 1은 이것으로 닫힙니다.

후보 하나에 며칠 쓰기 전에 root port 자체의 capability를 먼저 확인합니다:

```bash
sudo lspci -vv -s 00:01.0 | grep -iE "LnkCap:|LnkSta:"
sudo lspci -vv -s 01:00.0 | grep -iE "ASPM|LnkCtl:|LnkSta:"
```

context length는 원인이 아닙니다. hang을 인지한 시점에 쓰고 있던 profile을 탓하기 쉽지만, 카드가 버스에서 떨어져 재부팅이 필요했던 사례는 2026-08-06에 이미 기록됐습니다. 64K profile이 생기기 8일 전이고, 당시 기본값은 12288 / `auto` KV cache였습니다. 64K profile이 그래도 관여할 수 있는 경로는 상주 할당량 하나뿐입니다 — utilization을 0.85에서 0.95로 올리기 때문입니다. profile을 되돌리는 것만으로 검증되므로 비용이 없습니다.

이 host의 root port는 `ASPM not supported`를 보고합니다. 즉 GPU link에서 ASPM은 애초에 활성일 수 없었고, 끄는 것으로 달라지는 게 없습니다. root port는 `Width x8`도 보고하므로, 카드 쪽의 `x8 (downgraded)` 경고는 CPU lane 분기 결과이며 접점 불량이 아닙니다. 재장착으로는 아무것도 증명되지 않습니다.

대신 같은 출력이 보여주는 것: link가 유휴 시 2.5GT/s로 내려갔다가 부하 시 16GT/s로 재트레이닝됩니다. 카드가 최저 전력 상태에서 죽었다는 사실과 합치면, **저전력 전환 자체**가 가장 유력한 후보입니다.

1. **이 host에서 시험했고 실패했습니다.** 카드가 최저 유휴 상태로 내려가지 않게 고정합니다. P8 전력 상태와 유휴 link 다운시프트를 함께 억제합니다. 그럼에도 4시간 47분 뒤 44.7W / 510MHz / 0% utilization 상태에서 죽었습니다. lock이 실제로 걸려 있었음이 확인되므로 유휴 전력 상태는 원인이 아닙니다. 2026-08-24에 두 번째로 확인됐습니다 — 같은 lock, 같은 `P2`, 97.4시간 뒤 44.45W / 510MHz에서 사망. 레버를 쥔 채 독립적으로 두 번 재현됐으므로 여기에 더 시간을 쓰지 않습니다. 배제에 하루가 들고 레버가 직관적이지 않아 기록해 둡니다. **레버는 memory clock입니다.** graphics clock만 잠갔을 때는 SM 하한이 510MHz로 오른 것 외에 아무것도 바뀌지 않았습니다 — pstate는 P8, link는 2.5GT/s 그대로였습니다. memory clock을 잠그자 P2로 올라가고 link가 16GT/s로 유지됐습니다.

   `-lmc`는 지원되는 단계로 스냅되므로 최대값을 먼저 읽습니다:

   ```bash
   nvidia-smi --query-gpu=clocks.max.mem --format=csv,noheader
   ```

   crash로 재부팅되면 lock이 풀려 시험이 조용히 끝나버리므로 unit으로 설치합니다:

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

   service 상태만 보지 말고 세 지표를 모두 확인합니다:

   ```bash
   nvidia-smi --query-gpu=pstate,clocks.sm,clocks.mem,power.draw --format=csv,noheader
   sudo lspci -vv -s 00:01.0 | grep "LnkSta:"
   ```

   유휴 전력이 약 21W에서 44W로 오릅니다. 이것이 비용입니다.
2. PCIe 전력 관리 비활성화: kernel command line에 `pcie_aspm=off pcie_port_pm=off`. PCI-PM L1 substate도 같이 끄므로 유지할 값은 있지만, root port에 ASPM이 없는 환경에서 큰 기대는 하지 않습니다.
3. BIOS에서 Resizable BAR 비활성화.
4. BIOS에서 PCIe link 속도를 Gen3로 제한.
5. 카드와 전원 케이블 재장착, 12VHPWR connector 점검. 원래 이 항목에는 root port와 카드의 width가 불일치할 때만 의미가 있다고 적혀 있었습니다. 그 판정은 PCIe lane 접점에 대한 것이고 전원 공급은 포함하지 않습니다. 12VHPWR connector가 고장 나는 지점은 후자입니다. bare idle 결과가 나온 이상 link width와 무관하게 connector를 점검합니다.

이 host의 MTBF는 5분에서 97시간까지 편차가 큽니다. 양 극단 모두 2026-08-24에, 같은 clock lock이 걸린 상태에서 기록됐습니다. 이만큼 벌어진 편차는 단일 run으로 아무것도 입증하지도 반증하지도 못하며, 48시간 규칙이 있던 이유가 그것입니다. 동시에 카드가 악화되고 있다는 뜻이기도 합니다 — 97시간, 그 다음 부팅에서 5분. 간격이 분 단위가 되면 후보 하나를 이틀이 아니라 한 시간에 판정할 수 있으므로, 카드를 반송하기 전에 남은 BIOS 후보를 싸게 태울 수 있습니다.

host 레벨 후보가 소진되면 backend를 완전히 내리고 `gpu-watch`만 남깁니다. 지금까지의 모든 hang은 0% utilization 상태에서, vLLM이 23.5 GiB를 점유한 채로 발생했습니다. bare idle은 남은 두 가설을 가릅니다 — CUDA context가 전혀 없는 상태에서도 죽으면 하드웨어 결함이고 RMA 대상입니다. 비어 있는 상태로 며칠 버티면 vLLM의 연산이 아니라 상주 할당 자체를 봐야 합니다.

**이 실험은 이미 돌았고, 하드웨어로 답이 나왔습니다.** 2026-08-24, 콜드 부팅 직후의 host에서 vLLM을 한 번도 띄우지 않은 상태로, 드라이버 적재 5분 9초 만에 카드가 죽었습니다. `Xid` 직전 마지막 sample:

```
14:23:05.954, 41, 44.35 W, 510 MHz, 81 MiB, 0 %, P0
14:23:13      NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.
```

81 MiB는 `gnome-shell`이고 그 외에는 없습니다. 온도는 콜드 부팅에서 분당 1도씩 올라 41℃, 전력은 직전 2분간 44.3~44.7W로 평탄했고 어떤 종류의 스파이크도 없었습니다. 같은 날 10:11:47의 `Xid`와 달리 이번에는 `pid=`나 `name=` 귀속이 없습니다 — 탓할 프로세스 자체가 없었습니다. vLLM도, profile도, context length도, 상주 할당도 이 결과 앞에서는 남지 않습니다. 카드는 아무것도 돌지 않는 상태에서 죽습니다. 소프트웨어 튜닝을 멈추고 RMA를 엽니다.

이번 고장에서는 커널이 GPU crash dump도 생성했습니다. 이전 고장들에는 없던 것입니다. 모듈이 내려가면 사라지므로 재부팅 전에 수집합니다:

```bash
nvidia-bug-report.sh --output-file nvidia-bug-report-$(date +%Y%m%d)-xid79.log.gz
```

스크립트가 `.gz`를 한 번 더 붙이므로 실제 파일명은 `...log.gz.gz`가 됩니다.

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
