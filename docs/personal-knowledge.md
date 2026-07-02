# Personal Knowledge Tools

[Korean](personal-knowledge.ko.md)

Use read-only local file tools from `agent-gateway` to search configured folders and read short file excerpts.

## Tools

- `search_files`: keyword search over configured local paths.
- `read_file_excerpt`: reads a small line range from one matched file.

The tools are disabled by default.

## Enable

Mount one host folder into the gateway container and enable file search:

```text
FILE_SEARCH_ENABLED=true
FILE_SEARCH_POLICY=keyword
FILE_SEARCH_HOST_PATH=/path/on/host
FILE_SEARCH_CONTAINER_PATH=/knowledge
```

Add these values to the profile env file you run, or copy a profile to `.env` and append them:

```bash
cp profiles/vllm-qwen3-14b-awq.env .env
# edit .env, then add the FILE_SEARCH_* values above
docker compose --profile vllm up -d --build
```

`FILE_SEARCH_PATHS` inside the container is set from `FILE_SEARCH_CONTAINER_PATH` by `docker-compose.yml`.

## Policies

- `WEB_SEARCH_POLICY=keyword|question|always|off`
- `FILE_SEARCH_POLICY=keyword|always|off`
- `FETCH_URL_ENABLED=true|false`
- `FILE_SEARCH_ENABLED=true|false`

## Safety

- File search is read-only.
- Files must be under configured paths.
- Large files are skipped by `FILE_SEARCH_MAX_FILE_BYTES`.
- Binary files are skipped.
- Common generated folders such as `.git`, `node_modules`, `build`, and `dist` are skipped.

Search is keyword-based and limited to configured read-only paths.
