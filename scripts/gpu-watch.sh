#!/usr/bin/env bash
# Records GPU telemetry so the next hang leaves evidence instead of a guess.
# Kernel Xid messages are left to journald; `report` reads them back.
set -u

LOG_DIR="${GPU_WATCH_LOG_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/local-llm-starter}"
TELEMETRY="${LOG_DIR}/gpu-telemetry.csv"
# 15s, not 60s: the card died at idle 45s after its last sample, so a coarse
# interval is all blind spot and no signal.
INTERVAL="${GPU_WATCH_INTERVAL:-15}"
CONTAINER="${GPU_WATCH_CONTAINER:-vllm}"

QUERY="timestamp,temperature.gpu,power.draw,clocks.sm,memory.used,utilization.gpu,pstate,clocks_event_reasons.active"
# AER and pcieport lines matter as much as Xid: a card that drops off the bus
# while idle is a PCIe link story, not a CUDA one.
XID_PATTERN='Xid|fallen off the bus|NVRM:|GPU has fallen|AER|pcieport|[Ll]ink is [Dd]own'

usage() {
  cat >&2 <<EOF
usage: $0 <watch|report>

  watch   sample nvidia-smi every ${INTERVAL}s into ${TELEMETRY}
          run it under systemd or nohup so it survives your shell
  report  show what the last hang left behind

env: GPU_WATCH_LOG_DIR, GPU_WATCH_INTERVAL, GPU_WATCH_CONTAINER
EOF
  exit 2
}

section() {
  printf '\n=== %s ===\n' "$1"
}

watch_gpu() {
  mkdir -p "${LOG_DIR}" || exit 1

  # Older drivers spell this field clocks_throttle_reasons.active. A wrong field
  # name fails the whole query, so probe once instead of logging nothing for days.
  if ! nvidia-smi --query-gpu="${QUERY}" --format=csv,noheader >/dev/null 2>&1; then
    QUERY="${QUERY/clocks_event_reasons.active/clocks_throttle_reasons.active}"
    if ! nvidia-smi --query-gpu="${QUERY}" --format=csv,noheader >/dev/null 2>&1; then
      echo "gpu-watch: nvidia-smi cannot serve the query fields" >&2
      exit 1
    fi
  fi

  # One header, then one append per sample. Appending per sample (instead of
  # nvidia-smi's own -l loop) keeps stdio buffering from swallowing the final
  # readings when the card dies mid-buffer.
  if [[ ! -s "${TELEMETRY}" ]]; then
    printf '%s\n' "${QUERY}" >"${TELEMETRY}"
  fi

  printf 'gpu-watch: sampling every %ss into %s\n' "${INTERVAL}" "${TELEMETRY}" >&2

  while :; do
    if ! nvidia-smi --query-gpu="${QUERY}" --format=csv,noheader >>"${TELEMETRY}" 2>/dev/null; then
      # The card going missing is the single most useful line in this file.
      printf '%s, NVIDIA-SMI FAILED\n' "$(date -Is)" >>"${TELEMETRY}"
    fi
    sync "${TELEMETRY}" 2>/dev/null || sync
    sleep "${INTERVAL}"
  done
}

report_journal() {
  section "kernel Xid / NVRM"

  if ! command -v journalctl >/dev/null 2>&1; then
    echo "journalctl not found; try: dmesg | grep -iE '${XID_PATTERN}'"
    return
  fi

  if [[ ! -d /var/log/journal ]]; then
    echo "WARNING: journald is not persistent, so a reboot erases the Xid that"
    echo "explains the hang. Enable it once:"
    echo "  sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald"
  fi

  local boot
  for boot in -1 0; do
    printf -- '--- boot %s ---\n' "${boot}"
    if ! journalctl -k -b "${boot}" --no-pager -q 2>/dev/null |
      grep -iE "${XID_PATTERN}" | tail -n 20; then
      echo "(nothing, or boot ${boot} is not in the journal)"
    fi
  done

  cat <<'EOF'

Xid 79  = card dropped off the PCIe bus (power, PCIe link, thermal)
Xid 13/31 = bad kernel or memory access from a CUDA kernel
Xid 48  = uncorrectable VRAM error

Check the utilization column on the samples before the gap. An Xid 79 while
the card sat at 0% and its lowest clocks is a hardware, link, or power-state
fault; no vLLM setting causes it.
EOF
}

report_telemetry() {
  section "last telemetry before the gap"

  if [[ ! -s "${TELEMETRY}" ]]; then
    echo "no telemetry at ${TELEMETRY}; was 'gpu-watch.sh watch' running?"
    return
  fi

  tail -n 15 "${TELEMETRY}"
}

report_container() {
  section "${CONTAINER} container"

  if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found"
    return
  fi

  # fp8 KV cache can route attention through FlashInfer, the library whose
  # sampler kernel already took this card off the bus once.
  echo "--- attention backend ---"
  docker logs "${CONTAINER}" 2>&1 | grep -iE 'attention backend|using .* backend' |
    tail -n 3 || echo "(not found)"

  echo "--- recent errors ---"
  docker logs --tail 400 "${CONTAINER}" 2>&1 |
    grep -iE 'error|cuda|traceback|illegal|timeout' | tail -n 20 ||
    echo "(none)"
}

case "${1:-}" in
  watch) watch_gpu ;;
  report)
    report_journal
    report_telemetry
    report_container
    ;;
  *) usage ;;
esac
