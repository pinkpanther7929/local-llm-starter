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

## Automatic Perforce refresh before local search

The Qwen3.8 56K profile enables Korean/English local-source triggers and mounts
`/opt/TS_BuildMachine-10.6.6.56_main` read-only at `/knowledge`. A host service
syncs that fixed workspace before the first file tool in each chat request.
No LLM-supplied shell commands, depot paths, force sync, revert, or submit are accepted.
Sync failures block local reads for that request. Ordinary chat does not sync.
Explicit web requests can still search the web. File search skips Unreal generated
directories and files larger than 256 KiB by default; it is not a full indexed RAG system.

One-time setup on the Linux host (not inside the gateway):

```bash
cd /opt/TS_BuildMachine-10.6.6.56_main
command -v p4
p4 info
p4 login -s
p4 client -o
```

Use the actual executable, server, user, client, ticket and trust paths in the
configuration below. Authenticate interactively with `p4 login` if required;
never put passwords or ticket contents in Git. The service runs as root, matching
the current host setup; configure its ticket paths explicitly. Client `Root` must
match the workspace, options must include `noclobber noallwrite`, and there must
be no opened files. The broker refuses unsafe settings; it does not change them.
Use a dedicated read-only P4 account/workspace if this is also a live build workspace.
Safe `p4 sync -s` updates/removes files according to the configured client view;
large asset updates can take time. It does not use `-f`.

```bash
cd /opt/local-llm-starter
# Initial setup only: do not overwrite an existing configured env file.
sudo test -e /etc/local-llm-p4-sync.env || sudo install -m 600 scripts/p4-sync.env.example /etc/local-llm-p4-sync.env
sudoedit /etc/local-llm-p4-sync.env
sudo install -m 644 scripts/p4-sync.service /etc/systemd/system/local-llm-p4-sync.service
sudo systemctl daemon-reload
sudo systemctl enable --now local-llm-p4-sync
sudo systemctl status local-llm-p4-sync --no-pager

docker compose --env-file profiles/vllm-qwen3.8-27b-awq-56k.env \
  --profile vllm up -d --build --force-recreate --no-deps agent-gateway
```

Ask `저장소에서 CharacterMovement 구현 찾아줘`. The gateway requests sync once,
then searches and reads relevant excerpts. Model-initiated file calls use the same
sync guard. Concurrent sync requests fail explicitly rather than start duplicate
syncs. Inspect `journalctl -u local-llm-p4-sync -n 50 --no-pager` and gateway logs.
The gateway's 600-second broker wait and broker's 480-second per-command timeout
are finite; failures never fall back silently to stale files. The broker uses only
a root-owned Unix socket, not TCP, SSH, or the Docker socket. Start the host broker
before the gateway so the socket directory exists with restrictive permissions.

`P4_SYNC_ENABLED=false` disables refresh but keeps local search; results can then
be stale. `FILE_SEARCH_ENABLED=false` disables both search and its sync trigger.

## GPU Hang Evidence

A card that falls off the PCIe bus takes `nvidia-smi` with it, so the state
leading up to the hang has to be recorded before it happens.

Enable a persistent journal once, so the kernel `Xid` survives the reboot:

```bash
sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald
```

Then run the telemetry collector as a service:

```ini
# /etc/systemd/system/gpu-watch.service
[Unit]
Description=local-llm-starter GPU telemetry

[Service]
ExecStart=/opt/local-llm-starter/scripts/gpu-watch.sh watch
Environment=GPU_WATCH_LOG_DIR=/var/log/local-llm-starter
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gpu-watch
```

After the next hang, read everything back in one command:

```bash
GPU_WATCH_LOG_DIR=/var/log/local-llm-starter scripts/gpu-watch.sh report
```

It prints the kernel `Xid` from this boot and the previous one, the last
telemetry samples before the card disappeared, and the vLLM container's
attention backend plus recent errors.
