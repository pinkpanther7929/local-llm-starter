"""Host-only fixed-workspace sync broker. No shell or client-supplied commands."""
import json
import os
import re
import socketserver
import subprocess
import threading
from pathlib import Path

WORKSPACE = os.environ.get("P4_SYNC_WORKSPACE", "/opt/TS_BuildMachine-10.6.6.56_main")
SOCKET = os.environ.get("P4_SYNC_SOCKET", "/opt/local-llm-starter/runtime/p4-sync/sync.sock")
P4 = os.environ.get("P4_BIN", "/usr/local/bin/p4")
LOCK = threading.Lock()


def run_p4(*args):
    result = subprocess.run(
        [P4, "-d", WORKSPACE, "-s", *args], cwd=WORKSPACE,
        capture_output=True, text=True, errors="replace", timeout=480,
    )
    output = result.stdout + result.stderr
    if result.returncode or any(line.startswith("error:") for line in output.splitlines()):
        raise RuntimeError(output[-6000:] or "p4 failed")
    return output


def sync_workspace():
    # Refuse workspaces configured to overwrite writable files or with open edits.
    spec = run_p4("client", "-o")
    root = re.search(r"(?:^|\n)(?:(?:info\d*|text): )?Root:\s*([^\n]+)", spec)
    if not root or os.path.realpath(root.group(1).strip()) != os.path.realpath(WORKSPACE):
        raise RuntimeError("P4 client Root does not match the configured workspace.")
    options = re.search(r"(?:^|\n)(?:(?:info\d*|text): )?Options:\s*([^\n]+)", spec)
    if not options or not {"noclobber", "noallwrite"}.issubset(options.group(1).split()):
        raise RuntimeError("Workspace must use noclobber and noallwrite; no settings were changed.")
    run_p4("login", "-s")
    opened = run_p4("opened")
    if any(re.match(r"info\d*: //", line) for line in opened.splitlines()):
        raise RuntimeError("Workspace contains opened files; automatic sync refused.")
    run_p4("sync", "-s")
    return {"ok": True}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        if self.rfile.readline(16) != b"sync\n":
            return
        if not LOCK.acquire(blocking=False):
            result = {"ok": False, "error": "Another repository sync is running; retry after it finishes."}
        else:
            try:
                result = sync_workspace()
                print("P4 sync completed", flush=True)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[-1000:]}
                print("P4 sync failed: " + str(exc), flush=True)
            finally:
                LOCK.release()
        try:
            self.wfile.write((json.dumps(result) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    import fcntl
    path = Path(SOCKET)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep the descriptor open for the service lifetime, preventing two brokers.
    lock_file = open(path.parent / "service.lock", "a")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Single service instance; only remove a stale socket, never a regular file.
    if path.exists():
        if not path.is_socket():
            raise RuntimeError("Socket path is not a socket")
        path.unlink()
    with socketserver.ThreadingUnixStreamServer(str(path), Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    main()
