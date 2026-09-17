#!/bin/sh
set -eu

if [ -n "${LOCAL_MAX_NUM_SEQS:-}" ]; then
  set -- "$@" --max-num-seqs "$LOCAL_MAX_NUM_SEQS"
fi
if [ -n "${LOCAL_MAX_BATCHED_TOKENS:-}" ]; then
  set -- "$@" --max-num-batched-tokens "$LOCAL_MAX_BATCHED_TOKENS"
fi
if [ -n "${LOCAL_CPU_OFFLOAD_GB:-}" ]; then
  set -- "$@" --cpu-offload-gb "$LOCAL_CPU_OFFLOAD_GB"
fi
if [ "${VLLM_ENFORCE_EAGER:-0}" = "1" ]; then
  # vLLM requires the model positional argument immediately after `serve`.
  set -- "$@" --enforce-eager
fi

unset VLLM_ENFORCE_EAGER
exec vllm serve "$@"
