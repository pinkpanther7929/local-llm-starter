"""Tool service: the capabilities behind agent-gateway (web search, URL fetch, repository files).

agent-gateway owns the chat loop and decides nothing about *how* a tool runs; it fetches the
tool schemas from GET /tools and executes the calls the model asks for through POST /call.
Only this container mounts the repository (/knowledge) and the P4 sync socket, and only this
container reaches SearXNG, so tools can be added or removed without touching the gateway.

    GET  /health                 -> {"ok": true}
    GET  /tools                  -> {"tools": [OpenAI function schemas]}   (fixed order, stable prompt prefix)
    POST /call {name, arguments} -> {"ok": true, "result": ...} | {"ok": false, "error": "..."}

`X-Chat-Id` groups the calls of one chat request: the repository is synced at most once per id.
"""
import collections
import contextvars
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")
HOST = os.environ.get("TOOL_SERVICE_HOST", "0.0.0.0")
PORT = int(os.environ.get("TOOL_SERVICE_PORT", "8020"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "20"))
UPSTREAM_RETRIES = int(os.environ.get("UPSTREAM_RETRIES", "6"))
UPSTREAM_RETRY_DELAY_SECONDS = float(os.environ.get("UPSTREAM_RETRY_DELAY_SECONDS", "2"))
FETCH_MAX_BYTES = int(os.environ.get("FETCH_MAX_BYTES", "1048576"))
FETCH_MAX_CHARS = int(os.environ.get("FETCH_MAX_CHARS", "12000"))
SEARCH_RESULT_LIMIT = int(os.environ.get("SEARCH_RESULT_LIMIT", "5"))
# The *_POLICY variables only matter as an off switch now: the model decides when to call a tool.
WEB_SEARCH_POLICY = os.environ.get("WEB_SEARCH_POLICY", "keyword").lower()
FETCH_URL_ENABLED = os.environ.get("FETCH_URL_ENABLED", "true").lower() not in {"0", "false", "no"}
FILE_SEARCH_ENABLED = os.environ.get("FILE_SEARCH_ENABLED", "false").lower() in {"1", "true", "yes"}
FILE_SEARCH_POLICY = os.environ.get("FILE_SEARCH_POLICY", "keyword").lower()
P4_SYNC_ENABLED = os.environ.get("P4_SYNC_ENABLED", "false").lower() in {"1", "true", "yes"}
P4_SYNC_SOCKET = os.environ.get("P4_SYNC_SOCKET", "/p4-sync/sync.sock")
P4_SYNC_TIMEOUT_SECONDS = int(os.environ.get("P4_SYNC_TIMEOUT_SECONDS", "600"))
FILE_SYNC_STATE = contextvars.ContextVar("file_sync_state", default=None)
FILE_SEARCH_RESULT_LIMIT = int(os.environ.get("FILE_SEARCH_RESULT_LIMIT", "5"))
FILE_SEARCH_MAX_FILE_BYTES = int(os.environ.get("FILE_SEARCH_MAX_FILE_BYTES", "262144"))
FILE_SEARCH_MAX_CHARS = int(os.environ.get("FILE_SEARCH_MAX_CHARS", "1200"))
FILE_READ_MAX_LINES = int(os.environ.get("FILE_READ_MAX_LINES", "120"))
SYNC_STATE_KEEP = 256
FILE_SEARCH_EXTENSIONS = {
    item.strip().lower()
    for item in os.environ.get(
        "FILE_SEARCH_EXTENSIONS",
        ".txt,.md,.rst,.py,.ps1,.bat,.cmd,.sh,.json,.yaml,.yml,.toml,.ini,.cfg,.xml,.html,.css,.js,.ts,.cs,.cpp,.h,.hpp,.java,.go,.rs,.sql,.log",
    ).split(",")
    if item.strip()
}


def parse_path_list(value):
    paths = []
    for chunk in (value or "").replace("\n", ";").split(";"):
        path = chunk.strip()
        if path:
            paths.append(os.path.realpath(path))
    return paths


FILE_SEARCH_PATHS = parse_path_list(os.environ.get("FILE_SEARCH_PATHS", ""))
FILE_TOOLS_ENABLED = FILE_SEARCH_ENABLED and FILE_SEARCH_POLICY != "off"
WEB_TOOLS_ENABLED = WEB_SEARCH_POLICY != "off"

BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            text = " ".join((data or "").split())
            if text:
                self.parts.append(text)

    def text(self):
        return "\n".join(self.parts)



def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True


def read_json_request(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def request_json(url, timeout=HTTP_TIMEOUT_SECONDS, retries=UPSTREAM_RETRIES):
    last_error = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"Content-Type": "application/json; charset=utf-8"}, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
            return json.loads(text)
        except (ConnectionRefusedError, TimeoutError, urllib.error.URLError) as exc:
            # Retrying an invalid request cannot repair it.
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code not in {408, 429}:
                raise
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(UPSTREAM_RETRY_DELAY_SECONDS)
    raise last_error


def open_url(url, timeout=HTTP_TIMEOUT_SECONDS, max_bytes=FETCH_MAX_BYTES):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "local-llm-agent-gateway/1.0",
            "Accept": "text/html,text/plain,application/xhtml+xml,application/json;q=0.8,*/*;q=0.2",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        text = raw.decode("utf-8", errors="replace")
    return content_type, text, truncated


def normalize_hostname(hostname):
    return (hostname or "").strip().rstrip(".").lower()


def assert_public_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are allowed.")
    host = normalize_hostname(parsed.hostname)
    if not host or host in BLOCKED_HOSTS:
        raise ValueError("Blocked host.")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = [item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)]
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ValueError("Blocked private or non-public address.")


def tool_search_web(arguments):
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = int(arguments.get("limit") or SEARCH_RESULT_LIMIT)
    limit = max(1, min(limit, SEARCH_RESULT_LIMIT))
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    separator = "&" if "?" in SEARXNG_URL else "?"
    data = request_json(SEARXNG_URL + separator + params)
    results = []
    for item in data.get("results", [])[:limit]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "engine": item.get("engine", ""),
            }
        )
    return {"query": query, "results": results}


def html_to_text(html):
    parser = TextExtractor()
    parser.feed(html)
    return parser.text()


def tool_fetch_url(arguments):
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    assert_public_url(url)
    content_type, body, truncated = open_url(url)
    if "html" in content_type.lower():
        text = html_to_text(body)
    else:
        text = body
    text = text[:FETCH_MAX_CHARS]
    return {"url": url, "content_type": content_type, "truncated": truncated, "text": text}


SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".venv", "node_modules", "dist", "build", "Binaries", "Intermediate", "DerivedDataCache", "Saved", "SourceAssets", "ThirdParty"}
DEFERRED_HEAVY_DIRS = {
    item.strip().lower()
    for item in os.environ.get(
        "FILE_SEARCH_DEFERRED_DIRS",
        "engine,vendor,third_party,thirdparty,external",
    ).split(",")
    if item.strip()
}

SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "code", "file", "files",
    "find", "for", "from", "function", "class", "how", "in", "implementation",
    "is", "it", "line", "lines", "local", "of", "on", "only", "or", "path",
    "perforce", "read", "repo", "repository", "search", "show", "snippet",
    "source", "the", "to", "what", "where", "with",
    "검색", "검색해", "검색해줘", "관련", "구현", "내용", "대해", "대한",
    "로컬", "로컬파일", "메서드", "무엇", "뭔지", "보여줘", "부분", "설명",
    "설명해줘", "소스", "소스를", "소스코드", "어디", "어디에", "어떻게",
    "알려", "알려줘", "있나", "있는", "있는지", "있어", "저장소", "찾아",
    "찾아봐", "찾아줘", "코드", "코드를", "클래스", "파일", "파일내용",
    "퍼포스", "함수", "해줘", "확인", "확인해", "확인해줘",
}

KOREAN_PARTICLES = (
    "에서부터", "으로부터", "에서", "으로", "부터", "까지", "에게", "한테",
    "이랑", "하고", "보다", "처럼", "마다", "조차", "마저", "이나", "이란",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "도", "만", "과", "와",
)


def strip_korean_particle(token):
    for particle in KOREAN_PARTICLES:
        if token.endswith(particle) and len(token) - len(particle) >= 2:
            return token[: -len(particle)]
    return token


def extract_search_terms(query):
    raw_Text = (query or "").strip()
    if not raw_Text:
        return []
    # Split ASCII identifiers/paths cleanly from adjacent Hangul syllables so
    # "CharacterMovement에서" -> ["charactermovement", "에서"].
    chunks = re.findall(r"[A-Za-z0-9_./:-]+|[가-힣]+", raw_Text)
    cleaned = []
    filtered = []
    for chunk in chunks:
        token = chunk.strip("._/:-").lower()
        if not token:
            continue
        if re.match(r"^[가-힣]+$", token):
            token = strip_korean_particle(token)
        if len(token) < 2 and not token.isdigit():
            continue
        if token not in cleaned:
            cleaned.append(token)
        if token not in SEARCH_STOPWORDS and token not in KOREAN_PARTICLES and token not in filtered:
            filtered.append(token)
    return filtered or cleaned or [raw_Text.lower()]


def require_file_search_enabled():
    if not FILE_TOOLS_ENABLED:
        raise ValueError("Local file search is disabled.")
    if not FILE_SEARCH_PATHS:
        raise ValueError("FILE_SEARCH_PATHS is empty.")
    ensure_repository_synced()


def ensure_repository_synced():
    if not P4_SYNC_ENABLED:
        return
    state = FILE_SYNC_STATE.get()
    if state is not None and state.get("attempted"):
        if state.get("error"):
            raise ValueError(state["error"])
        return
    if state is not None:
        state["attempted"] = True
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(P4_SYNC_TIMEOUT_SECONDS)
            client.connect(P4_SYNC_SOCKET)
            client.sendall(b"sync\n")
            with client.makefile("rb") as response:
                result = json.loads(response.readline(16384))
        if not result.get("ok"):
            raise ValueError(result.get("error") or "P4 sync failed")
    except Exception as exc:
        error = "Repository sync failed; source was not searched: {}".format(exc)
        if state is not None:
            state["error"] = error
        raise ValueError(error) from exc


def is_within_root(path, root):
    try:
        common = os.path.commonpath([os.path.realpath(path), os.path.realpath(root)])
    except ValueError:
        return False
    return common == os.path.realpath(root)


def resolve_allowed_file(path):
    require_file_search_enabled()
    requested = str(path or "").strip()
    if not requested:
        raise ValueError("path is required")
    candidates = []
    if os.path.isabs(requested):
        candidates.append(os.path.realpath(requested))
    else:
        for root in FILE_SEARCH_PATHS:
            candidates.append(os.path.realpath(os.path.join(root, requested)))
    for candidate in candidates:
        if any(is_within_root(candidate, root) for root in FILE_SEARCH_PATHS) and os.path.isfile(candidate):
            return candidate
    raise ValueError("File is outside configured paths or does not exist.")


def resolve_search_roots(subpath=None):
    requested = str(subpath or "").strip()
    if not requested:
        return [root for root in FILE_SEARCH_PATHS if os.path.isdir(root)]
    candidates = []
    if os.path.isabs(requested):
        candidates.append(os.path.realpath(requested))
    else:
        for root in FILE_SEARCH_PATHS:
            candidates.append(os.path.realpath(os.path.join(root, requested)))
    matched = [
        candidate
        for candidate in candidates
        if any(is_within_root(candidate, root) for root in FILE_SEARCH_PATHS) and os.path.isdir(candidate)
    ]
    if not matched:
        raise ValueError("Directory is outside configured paths or does not exist.")
    return matched


def relative_to_allowed_root(path):
    for root in FILE_SEARCH_PATHS:
        if is_within_root(path, root):
            return os.path.relpath(path, root).replace("\\", "/")
    return path


def is_searchable_file(path):
    try:
        if os.path.getsize(path) > FILE_SEARCH_MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    _, ext = os.path.splitext(path)
    return ext.lower() in FILE_SEARCH_EXTENSIONS


def read_text_file(path, max_bytes=FILE_SEARCH_MAX_FILE_BYTES):
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if b"\x00" in raw:
        raise ValueError("Binary file skipped.")
    return raw[:max_bytes].decode("utf-8", errors="replace"), len(raw) > max_bytes


def score_file_candidate(relative_path, terms, content_matched_terms=(), content_hits=0):
    relative_lower = relative_path.lower()
    filename_lower = os.path.basename(relative_lower)
    stem_lower = os.path.splitext(filename_lower)[0]
    matched_terms = set(content_matched_terms)
    path_score = 0
    for term in terms:
        if term == stem_lower or term == filename_lower:
            path_score += 80
            matched_terms.add(term)
        elif term in filename_lower:
            path_score += 30
            matched_terms.add(term)
        elif term in relative_lower:
            path_score += 8
            matched_terms.add(term)
    score = path_score + (len(matched_terms) * 25) + min(int(content_hits), 15) * 2
    if score <= 0:
        return 0
    path_parts = [part for part in relative_lower.split("/") if part]
    is_deferred_path = any(part in DEFERRED_HEAVY_DIRS for part in path_parts[:-1])
    wants_deferred = any(term in DEFERRED_HEAVY_DIRS for term in terms)
    if is_deferred_path and not wants_deferred:
        score = max(1, int(score * 0.45))
    return score


def build_rg_globs():
    globs = []
    for skip in sorted(SKIP_DIRS):
        globs.extend(["-g", "!{}/**".format(skip)])
    for ext in sorted(FILE_SEARCH_EXTENSIONS):
        globs.extend(["-g", "*{}".format(ext)])
    return globs


def search_files_with_rg(rg_bin, roots, terms):
    globs = build_rg_globs()
    max_size = str(FILE_SEARCH_MAX_FILE_BYTES)
    candidates = {}

    # 1. Fast filename/path pass via rg --files
    files_cmd = [rg_bin, "--files", "--no-ignore", "--max-filesize", max_size] + globs + roots
    proc_files = subprocess.run(files_cmd, capture_output=True, text=True, errors="replace", timeout=30)
    for raw_line in proc_files.stdout.splitlines():
        path = raw_line.strip()
        if not path:
            continue
        rel = relative_to_allowed_root(path)
        rel_lower = rel.lower()
        if any(term in rel_lower for term in terms):
            candidates[path] = {
                "path": path,
                "relative_path": rel,
                "matched_terms": set(),
                "hits": 0,
                "snippets": [],
            }

    # 2. Fast content pass via rg --json -i -m 10
    pattern = "|".join(re.escape(term) for term in terms if term)
    if pattern:
        match_cmd = [
            rg_bin, "--json", "-i", "--no-ignore", "-m", "10",
            "--max-filesize", max_size,
        ] + globs + ["-e", pattern] + roots
        proc_match = subprocess.run(match_cmd, capture_output=True, text=True, errors="replace", timeout=60)
        for raw_line in proc_match.stdout.splitlines():
            if not raw_line.startswith("{"):
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path = (data.get("path") or {}).get("text")
            line_text = ((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            line_no = int(data.get("line_number") or 0)
            if not path:
                continue
            entry = candidates.get(path)
            if entry is None:
                entry = {
                    "path": path,
                    "relative_path": relative_to_allowed_root(path),
                    "matched_terms": set(),
                    "hits": 0,
                    "snippets": [],
                }
                candidates[path] = entry
            entry["hits"] += 1
            line_lower = line_text.lower()
            for term in terms:
                if term in line_lower:
                    entry["matched_terms"].add(term)
            if len(entry["snippets"]) < 3:
                entry["snippets"].append(
                    {
                        "line": line_no,
                        "text": line_text.strip()[:FILE_SEARCH_MAX_CHARS],
                    }
                )

    results = []
    for path, entry in candidates.items():
        score = score_file_candidate(
            entry["relative_path"],
            terms,
            entry["matched_terms"],
            entry["hits"],
        )
        if score <= 0:
            continue
        snippets = entry["snippets"] or [{"line": 0, "text": "Path match"}]
        results.append(
            {
                "path": path,
                "relative_path": entry["relative_path"],
                "score": score,
                "truncated": False,
                "snippets": snippets,
            }
        )
    return results


def search_files_with_walk(roots, terms):
    results = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for filename in filenames:
                path = os.path.join(current_root, filename)
                if not is_searchable_file(path):
                    continue
                relative_path = relative_to_allowed_root(path)
                try:
                    text, truncated = read_text_file(path)
                except (OSError, ValueError):
                    continue
                lowered = text.lower()
                matched_terms = {term for term in terms if term in lowered}
                hits = sum(min(lowered.count(term), 10) for term in matched_terms)
                score = score_file_candidate(relative_path, terms, matched_terms, hits)
                if score <= 0:
                    continue
                snippets = []
                for line_number, line in enumerate(text.splitlines(), start=1):
                    line_lower = line.lower()
                    if any(term in line_lower for term in terms):
                        snippets.append(
                            {
                                "line": line_number,
                                "text": line.strip()[:FILE_SEARCH_MAX_CHARS],
                            }
                        )
                    if len(snippets) >= 3:
                        break
                if not snippets:
                    snippets.append({"line": 0, "text": "Path match"})
                results.append(
                    {
                        "path": path,
                        "relative_path": relative_path,
                        "score": score,
                        "truncated": truncated,
                        "snippets": snippets,
                    }
                )
    return results


def split_primary_and_deferred_roots(roots):
    primary = []
    deferred = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            children = sorted(os.listdir(root))
        except OSError:
            primary.append(root)
            continue
        has_deferred = any(
            name.lower() in DEFERRED_HEAVY_DIRS and os.path.isdir(os.path.join(root, name))
            for name in children
        )
        if not has_deferred:
            primary.append(root)
            continue
        for name in children:
            if name in SKIP_DIRS:
                continue
            full = os.path.join(root, name)
            if name.lower() in DEFERRED_HEAVY_DIRS and os.path.isdir(full):
                deferred.append(full)
            else:
                primary.append(full)
    return primary, deferred


def run_search_backend(roots, terms):
    if not roots:
        return []
    rg_bin = shutil.which("rg")
    if rg_bin:
        try:
            return search_files_with_rg(rg_bin, roots, terms)
        except Exception:
            pass
    return search_files_with_walk(roots, terms)


def tool_search_files(arguments):
    require_file_search_enabled()
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(arguments.get("limit") or FILE_SEARCH_RESULT_LIMIT), FILE_SEARCH_RESULT_LIMIT))
    explicit_path = str(arguments.get("path") or "").strip()
    roots = resolve_search_roots(explicit_path)
    terms = extract_search_terms(query)
    wants_heavy = bool(explicit_path) or any(term in DEFERRED_HEAVY_DIRS for term in terms)
    if wants_heavy:
        results = run_search_backend(roots, terms)
    else:
        primary_roots, deferred_roots = split_primary_and_deferred_roots(roots)
        results = run_search_backend(primary_roots, terms)
        if len(results) < limit and deferred_roots:
            results.extend(run_search_backend(deferred_roots, terms))
    results.sort(key=lambda item: (item["score"], -len(item["relative_path"])), reverse=True)
    return {
        "query": query,
        "terms": terms,
        "roots": roots,
        "results": results[:limit],
    }


def tool_list_files(arguments):
    require_file_search_enabled()
    explicit_path = str(arguments.get("path") or "").strip()
    roots = resolve_search_roots(explicit_path)
    pattern = str(arguments.get("pattern") or "").strip().lower()
    limit = max(1, min(int(arguments.get("limit") or 30), 100))
    entries = []
    if not pattern:
        # List immediate children of the requested directory/roots
        for root in roots:
            try:
                names = sorted(os.listdir(root))
            except OSError:
                continue
            for name in names:
                if name in SKIP_DIRS:
                    continue
                full = os.path.join(root, name)
                rel = relative_to_allowed_root(full)
                entries.append(
                    {
                        "path": rel + ("/" if os.path.isdir(full) else ""),
                        "type": "dir" if os.path.isdir(full) else "file",
                    }
                )
                if len(entries) >= limit:
                    break
        return {"roots": roots, "pattern": pattern, "entries": entries[:limit]}

    rg_bin = shutil.which("rg")
    if rg_bin and roots:
        globs = build_rg_globs()
        wants_heavy = bool(explicit_path) or any(heavy in pattern for heavy in DEFERRED_HEAVY_DIRS)
        search_batches = [roots] if wants_heavy else list(split_primary_and_deferred_roots(roots))
        matched = []
        for batch in search_batches:
            if not batch:
                continue
            cmd = [rg_bin, "--files", "--no-ignore"] + globs + batch
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=30)
            for raw_line in proc.stdout.splitlines():
                full = raw_line.strip()
                if not full:
                    continue
                rel = relative_to_allowed_root(full)
                if pattern in rel.lower():
                    matched.append(rel)
            if len(matched) >= limit:
                break
        matched.sort(
            key=lambda rel: (
                any(part in DEFERRED_HEAVY_DIRS for part in rel.lower().split("/")[:-1]),
                len(rel),
                rel,
            )
        )
        return {
            "roots": roots,
            "pattern": pattern,
            "entries": [{"path": rel, "type": "file"} for rel in matched[:limit]],
        }

    for root in roots:
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for filename in filenames:
                full = os.path.join(current_root, filename)
                rel = relative_to_allowed_root(full)
                if pattern in rel.lower():
                    entries.append({"path": rel, "type": "file"})
                    if len(entries) >= limit:
                        return {"roots": roots, "pattern": pattern, "entries": entries}
    return {"roots": roots, "pattern": pattern, "entries": entries}


def tool_read_file_excerpt(arguments):
    path = resolve_allowed_file(arguments.get("path"))
    start_line = max(1, int(arguments.get("start_line") or 1))
    max_lines = max(1, min(int(arguments.get("max_lines") or FILE_READ_MAX_LINES), FILE_READ_MAX_LINES))
    text, truncated = read_text_file(path)
    lines = text.splitlines()
    start_index = min(start_line - 1, len(lines))
    selected = lines[start_index : start_index + max_lines]
    return {
        "path": path,
        "relative_path": relative_to_allowed_root(path),
        "start_line": start_index + 1 if lines else 1,
        "end_line": start_index + len(selected),
        "truncated": truncated,
        "text": "\n".join(selected),
    }



TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the public web with SearXNG and return result titles, URLs, and snippets. Use it for current or external information you do not already know; cite the source URLs in the final answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {"type": "integer", "description": "Maximum result count, 1-5."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch readable text from a public http/https URL, for example a search_web result you need to read in full. Private, loopback, and link-local hosts are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public URL to read."},
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "search_web": tool_search_web,
    "fetch_url": tool_fetch_url,
}

if not WEB_TOOLS_ENABLED:
    TOOLS = [tool for tool in TOOLS if tool.get("function", {}).get("name") not in {"search_web", "fetch_url"}]
    TOOL_HANDLERS.pop("search_web", None)
    TOOL_HANDLERS.pop("fetch_url", None)
elif not FETCH_URL_ENABLED:
    TOOLS = [tool for tool in TOOLS if tool.get("function", {}).get("name") != "fetch_url"]
    TOOL_HANDLERS.pop("fetch_url", None)

if FILE_TOOLS_ENABLED:
    TOOLS.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List directories or find files by filename substring in the configured source repository (read-only). Use it when the user asks about repository, source code, or local files. If it fails, report the failure; never claim the latest source was verified.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Optional subdirectory relative to a configured root (e.g. 'src/core')."},
                            "pattern": {"type": "string", "description": "Optional filename/path substring filter."},
                            "limit": {"type": "integer", "description": "Maximum result count."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "Search file contents in the configured source repository (read-only) and return matching files with short line snippets. Use it when the user asks about repository, source code, functions, classes, or local files, including Korean questions (저장소, 소스, 코드, 함수). Search concise identifiers or filenames, not a whole sentence. If it fails, report the failure; never claim the latest source was verified.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Concise identifier, symbol, or keyword query."},
                            "path": {"type": "string", "description": "Optional subdirectory relative to a configured root (e.g. 'src')."},
                            "limit": {"type": "integer", "description": "Maximum result count."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file_excerpt",
                    "description": "Read a small line range from a file in the configured source repository (read-only), typically after search_files located it.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Absolute path or path relative to a configured root."},
                            "start_line": {"type": "integer", "description": "1-based start line."},
                            "max_lines": {"type": "integer", "description": "Maximum lines to read."},
                        },
                        "required": ["path"],
                    },
                },
            },
        ]
    )
    TOOL_HANDLERS.update(
        {
            "list_files": tool_list_files,
            "search_files": tool_search_files,
            "read_file_excerpt": tool_read_file_excerpt,
        }
    )


def parse_tool_arguments(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(value)


def run_tool_call(tool_call):
    function = tool_call.get("function") or {}
    name = function.get("name") or ""
    arguments = parse_tool_arguments(function.get("arguments"))
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError("Unknown tool: {}".format(name))
    return handler(arguments)



# One chat request may call several tools; the repository is synced at most once for all of them.
# The gateway sends the same X-Chat-Id for every call of a chat request. Without an id, every call
# is its own request and syncs on its own.
_SYNC_STATES = collections.OrderedDict()
_SYNC_LOCK = threading.Lock()


def sync_state_for(chat_id):
    if not chat_id:
        return {}
    with _SYNC_LOCK:
        state = _SYNC_STATES.get(chat_id)
        if state is None:
            state = _SYNC_STATES[chat_id] = {}
            while len(_SYNC_STATES) > SYNC_STATE_KEEP:
                _SYNC_STATES.popitem(last=False)
        else:
            _SYNC_STATES.move_to_end(chat_id)
        return state


def call_tool(name, arguments, chat_id=""):
    token = FILE_SYNC_STATE.set(sync_state_for(chat_id))
    try:
        return run_tool_call({"function": {"name": name, "arguments": arguments}})
    finally:
        FILE_SYNC_STATE.reset(token)


class Handler(BaseHTTPRequestHandler):
    server_version = "local-llm-tool-service/1.0"

    def log_message(self, fmt, *args):
        print("{} - {}".format(self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            json_response(self, 200, {"ok": True, "tools": sorted(TOOL_HANDLERS)})
        elif path == "/tools":
            json_response(self, 200, {"tools": TOOLS})
        else:
            json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path != "/call":
            json_response(self, 404, {"error": "not found"})
            return
        try:
            request = read_json_request(self)
        except json.JSONDecodeError as exc:
            json_response(self, 400, {"ok": False, "error": "invalid json: {}".format(exc)})
            return
        name = str(request.get("name") or "")
        try:
            result = call_tool(name, request.get("arguments"), self.headers.get("X-Chat-Id", ""))
            json_response(self, 200, {"ok": True, "result": result})
        except Exception as exc:  # A failed tool is a result for the model, not a service error.
            json_response(self, 200, {"ok": False, "error": str(exc)})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("tool-service listening on {}:{} tools={}".format(HOST, PORT, sorted(TOOL_HANDLERS)), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
