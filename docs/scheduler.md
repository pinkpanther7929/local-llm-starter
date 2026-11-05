# LLM Scheduler

English | [Korean](scheduler.ko.md)

`llm-scheduler` sits in front of the Strata backend and orders generation requests by priority, so several
callers (wiki-relay, agents, Jenkins, other teams' batch jobs, Open WebUI) can share one GPU without
starving interactive requests. It does not rewrite requests or responses.

```text
callers --> llm-scheduler --> strata (one request at a time by default)
Open WebUI --> agent-gateway --> llm-scheduler
```

## Behavior

- `POST /v1/chat/completions` and `POST /v1/completions` go through a priority queue. Everything else
  (`GET /v1/models`, ...) is proxied immediately.
- Request and response bodies are forwarded unchanged. SSE streams are relayed as chunks arrive.
- At most `SCHED_CONCURRENCY` generations run at once (default `1`; Strata is fastest when it runs one at a time).
- A request waiting longer than `SCHED_AGING_SEC` moves up one level per interval, so P2 never starves.
- A client that disconnects while queued is removed from the queue. An upstream failure returns `502` and frees the slot.
- Running generations are never interrupted; "priority" decides who gets the next free slot.

## Priority

Send two optional headers. The body and URL stay the same.

| Header | Value | Meaning |
| --- | --- | --- |
| `X-LLM-Priority` | `P0` | A person is waiting (interactive chat, manual rewrite) |
| | `P1` | Pipeline work (commit drafts, CI failure summaries) |
| | `P2` | Background batch (indexing, crawling) |
| `X-Caller` | any short name | Label for the status page only. No effect on ordering |

Without `X-LLM-Priority` the source-IP map `SCHED_IP_PRIORITY` applies, then `SCHED_DEFAULT_PRIORITY` (`P2`).
The header is trusted as sent; this is intended for a trusted LAN. Watch `/v1/scheduler/status` for callers that abuse `P0`.

```bash
curl http://10.6.6.56:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-LLM-Priority: P0' -H 'X-Caller: my-service' \
  -d '{"model":"qwen3.8-flash-next-iq3_s","messages":[{"role":"user","content":"hi"}]}'
```

Current callers:

| Caller | Priority |
| --- | --- |
| wiki-relay manual rewrite / revert | P0 |
| wiki-relay commit draft | P1 |
| wiki-relay document card indexing | P2 |
| Open WebUI via agent-gateway | P0 (needs `UPSTREAM_BASE_URL` pointing at the scheduler) |

## Configuration (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `SCHED_PORT` | `8000` | Host port (Strata is no longer published on the host) |
| `SCHED_CONCURRENCY` | `1` | Concurrent generations sent to Strata |
| `SCHED_AGING_SEC` | `120` | Seconds of waiting per one-level promotion. `0` disables aging |
| `SCHED_DEFAULT_PRIORITY` | `P2` | Priority when no header and no IP match |
| `SCHED_IP_PRIORITY` | empty | Default per source IP, e.g. `10.6.5.155=P2,10.6.6.10=P1` |
| `SCHED_UPSTREAM` | `http://strata:8000` | Backend URL |
| `SCHED_UPSTREAM_TIMEOUT` | `900` | Upstream read timeout in seconds |

## Run and check

```bash
docker compose --profile strata up -d --build llm-scheduler
scripts/scheduler-smoke.sh http://10.6.6.56:8000
curl http://10.6.6.56:8000/v1/scheduler/status
```

`/v1/scheduler/status` returns the running request, the queue (with effective priority after aging), and per-caller
totals: requests, errors, cancelled, average wait/run seconds, completion tokens, tok/s.

## Rollout

The compose file ships the final layout: `llm-scheduler` publishes host port `8000` (`SCHED_PORT`), and `strata`
is only exposed on the Docker network. Other teams calling `10.6.6.56:8000` need no change; headerless requests
become `P2`.

1. Tell other teams first (headers to send, headerless requests become `P2`, Strata is recreated so expect a
   1-2 minute gap).
2. In `.env` set `UPSTREAM_BASE_URL=http://llm-scheduler:8000/v1` (agent-gateway). Otherwise Open WebUI reaches
   Strata directly over the Docker network and bypasses the queue.
3. `docker compose --profile strata up -d --build`, then `scripts/scheduler-smoke.sh` and check
   `/v1/scheduler/status`.
4. **Rollback**: give `strata` back `ports: ["8000:8000"]`, set `SCHED_PORT=8020` and
   `UPSTREAM_BASE_URL=http://strata:8000/v1`, then `docker compose --profile strata up -d`. To stop only the
   scheduler: `docker compose --profile strata rm -sf llm-scheduler` (callers then need Strata published again).

## Notes

- The scheduler belongs to the `strata` profile. It is not used with the `vllm` or `ollama` profiles.
- Status data is in memory and resets on restart.
- Tests: `python -m unittest scheduler.test_llm_scheduler`
