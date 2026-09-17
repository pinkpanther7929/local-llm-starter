#!/bin/sh
set -eu

if [ -n "${VLLM_ENFORCE_EAGER:-}" ]; then
  exec vllm serve --enforce-eager "$@"
fi

exec vllm serve "$@"
