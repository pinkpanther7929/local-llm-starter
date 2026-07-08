# Operations

English | [Korean](operations.ko.md)

Use these scripts after cloning the repository on a new machine and after changing profiles or ports.

## Host Readiness

```bash
cp .env.example .env
scripts/check-host.sh
```

The script checks Docker, Docker Compose, NVIDIA visibility, common ports, disk space, Compose rendering, and the local file mount path when file search is enabled.

Use a specific env file:

```bash
scripts/check-host.sh --env-file profiles/vllm-qwen3-14b-awq.env
```

## Runtime Smoke Test

Start one backend profile first:

```bash
docker compose --profile vllm up -d --build
```

Then run:

```bash
scripts/smoke-test.sh
```

The script checks:

- agent gateway `/health`
- agent gateway `/v1/models`
- backend `/v1/models`
- one chat completion through the gateway
- SearXNG JSON search
- Open WebUI HTTP response

Use a remote host:

```bash
SMOKE_HOST=10.6.6.56 scripts/smoke-test.sh
```

Skip the chat completion when the model is still loading:

```bash
SMOKE_SKIP_CHAT=true scripts/smoke-test.sh
```

Increase timeouts for cold model starts:

```bash
SMOKE_CHAT_TIMEOUT=180 scripts/smoke-test.sh
```

## Large Local Folder Mounts

For large source trees, keep the mount read-only and point the gateway at the host path:

```env
FILE_SEARCH_ENABLED=true
FILE_SEARCH_HOST_PATH=/opt/my-source-tree
FILE_SEARCH_CONTAINER_PATH=/knowledge
```

Restart only the gateway when changing gateway code or file-search environment:

```bash
docker compose up -d --build agent-gateway
```

Check the mount:

```bash
docker exec local-llm-agent-gateway ls /knowledge
```
