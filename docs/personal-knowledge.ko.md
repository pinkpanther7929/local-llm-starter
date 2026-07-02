# 개인 지식 도구

[English](personal-knowledge.md)

`agent-gateway`에서 설정된 폴더를 검색하고 짧은 파일 일부를 읽기 위한 read-only local file tool입니다.

## 도구

- `search_files`: 설정된 local path에서 keyword search.
- `read_file_excerpt`: 검색된 파일의 작은 line range 읽기.

기본값은 비활성화입니다.

## 활성화

Host folder 하나를 gateway container에 mount하고 file search를 켭니다:

```text
FILE_SEARCH_ENABLED=true
FILE_SEARCH_POLICY=keyword
FILE_SEARCH_HOST_PATH=/path/on/host
FILE_SEARCH_CONTAINER_PATH=/knowledge
```

이 값을 실행할 profile env file에 추가하거나, profile을 `.env`로 복사한 뒤 값을 추가합니다:

```bash
cp profiles/vllm-qwen3-14b-awq.env .env
# .env를 수정하고 위 FILE_SEARCH_* 값을 추가합니다
docker compose --profile vllm up -d --build
```

Container 내부 `FILE_SEARCH_PATHS`는 `docker-compose.yml`에서 `FILE_SEARCH_CONTAINER_PATH`로 설정합니다.

## Policy

- `WEB_SEARCH_POLICY=keyword|question|always|off`
- `FILE_SEARCH_POLICY=keyword|always|off`
- `FETCH_URL_ENABLED=true|false`
- `FILE_SEARCH_ENABLED=true|false`

## 안전장치

- File search는 read-only입니다.
- 파일은 configured path 아래에 있어야 합니다.
- 큰 파일은 `FILE_SEARCH_MAX_FILE_BYTES` 기준으로 skip합니다.
- Binary file은 skip합니다.
- `.git`, `node_modules`, `build`, `dist` 같은 generated folder는 skip합니다.

검색은 keyword 기반이며, 설정된 read-only path 안에서만 동작합니다.
