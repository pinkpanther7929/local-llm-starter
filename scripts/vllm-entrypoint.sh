#!/bin/sh
set -eu

if [ -n "${VLLM_ENFORCE_EAGER:-}" ]; then
  # vLLM requires the model positional argument immediately after `serve`.
  exec vllm serve "$@" --enforce-eager
fi

exec vllm serve "$@"
