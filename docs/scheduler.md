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
curl http://10.6.6.56:8020/v1/chat/completions \
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
| Open WebUI via agent-gateway | P0 (after cutover, see below) |

## Configuration (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `SCHED_PORT` | `8020` | Host port. Use `8000` after cutover |
| `SCHED_CONCURRENCY` | `1` | Concurrent generations sent to Strata |
| `SCHED_AGING_SEC` | `120` | Seconds of waiting per one-level promotion. `0` disables aging |
| `SCHED_DEFAULT_PRIORITY` | `P2` | Priority when no header and no IP match |
| `SCHED_IP_PRIORITY` | empty | Default per source IP, e.g. `10.6.5.155=P2,10.6.6.10=P1` |
| `SCHED_UPSTREAM` | `http://strata:8000` | Backend URL |
| `SCHED_UPSTREAM_TIMEOUT` | `900` | Upstream read timeout in seconds |

## Run and check

```bash
docker compose --profile strata up -d --build llm-scheduler
scripts/scheduler-smoke.sh http://10.6.6.56:8020
curl http://10.6.6.56:8020/v1/scheduler/status
```

`/v1/scheduler/status` returns the running request, the queue (with effective priority after aging), and per-caller
totals: requests, errors, cancelled, average wait/run seconds, completion tokens, tok/s.

## Rollout

1. **Trial (current)**: scheduler on `:8020`, Strata still directly reachable on `:8000`. Point one caller
   (for example wiki-relay `llm_endpoint`) at `:8020` and verify. Callers on `:8000` bypass the scheduler,
   so GPU contention with them is unchanged in this phase.
2. **Cutover**: make `:8000` the only entrance.
   1. Tell other teams using `10.6.6.56:8000` first (headers to send, headerless requests become `P2`).
   2. In `docker-compose.yml`, replace the `ports:` entry of `strata` with `expose: ["8000"]`.
   3. Set `SCHED_PORT=8000` in `.env`.
   4. Set `UPSTREAM_BASE_URL=http://llm-scheduler:8000/v1` for `agent-gateway`, otherwise Open WebUI reaches Strata
      directly over the Docker network and bypasses the queue. Add `X-LLM-Priority: P0` / `X-Caller: open-webui`
      to the gateway's upstream requests.
   5. `docker compose --profile strata up -d`
3. **Rollback**: restore the `strata` `ports:` entry, set `SCHED_PORT=8020`, restore `UPSTREAM_BASE_URL`, and
   `docker compose --profile strata up -d`. Or stop only the scheduler: `docker compose --profile strata rm -sf llm-scheduler`.

## Notes

- The scheduler belongs to the `strata` profile. It is not used with the `vllm` or `ollama` profiles.
- Status data is in memory and resets on restart.
- Tests: `python -m unittest scheduler.test_llm_scheduler`
