#!/usr/bin/env python3
"""
fake_server.py - Fake HTTP download server for exercising cfd.bat retry logic.

Modes:
    python fake_server.py                 # interactive server on port 8765
    python fake_server.py --port 9000     # custom port
    python fake_server.py --test          # automated scenarios -> ./Tests/

Interactive mode prints cfd commands you can paste into a shell.

Server endpoints:
    GET /file/<name>                               serve or fail
    GET /configure?name=<n>&fail_count=<k>         make <n> fail the next k hits
    GET /state                                     JSON snapshot
    GET /reset                                     clear state

Test mode:
    * starts the server on an ephemeral port
    * creates ./Tests/<timestamp>/ with one subfolder per scenario
    * writes a prepared .cfd.queue, runs `cfd.bat __worker__ <dir>`
    * captures worker stdout, final queue state, downloaded files
    * writes summary.txt across all scenarios

The test runner sets CFD_SMALL_DELAY=0 and CFD_WORKER_EXIT_DELAY=0 so
scenarios complete in seconds. Real runs use the bat's defaults (10s
between retries, 5s linger on empty queue).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent
TESTS_DIR = REPO_ROOT / "Tests"

CONTENT_MULTIPLIER = 64  # how many lines of content to serve


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class ServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fail_counts: dict[str, int] = {}
        self.request_log: list[dict] = []

    def configure(self, name: str, fail_count: int) -> None:
        with self.lock:
            self.fail_counts[name] = max(0, int(fail_count))

    def reset(self) -> None:
        with self.lock:
            self.fail_counts.clear()
            self.request_log.clear()

    def register_request(self, name: str, path: str) -> bool:
        """Record a request and return True if it should fail."""
        with self.lock:
            remaining = self.fail_counts.get(name, 0)
            should_fail = remaining > 0
            if should_fail:
                self.fail_counts[name] = remaining - 1
            self.request_log.append(
                {
                    "time": time.time(),
                    "name": name,
                    "path": path,
                    "result": "fail" if should_fail else "success",
                    "fail_remaining_after": self.fail_counts.get(name, 0),
                }
            )
            return should_fail

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "fail_counts": dict(self.fail_counts),
                "requests": list(self.request_log),
            }


STATE = ServerState()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class FakeHandler(BaseHTTPRequestHandler):
    server_version = "cfd-fake/1.0"

    def log_message(self, fmt, *args):
        pass

    def _respond(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/state":
            self._respond(200, json.dumps(STATE.snapshot(), indent=2).encode(),
                          "application/json")
            return
        if path == "/reset":
            STATE.reset()
            self._respond(200, b"ok\n")
            return
        if path == "/configure":
            name = (query.get("name") or [""])[0]
            try:
                count = int((query.get("fail_count") or ["0"])[0])
            except ValueError:
                count = 0
            if name:
                STATE.configure(name, count)
            self._respond(200, f"configured {name} fail_count={count}\n".encode())
            return
        if path.startswith("/file/"):
            self._serve_file(path[len("/file/"):])
            return

        self._respond(404, b"not found\n")

    def _serve_file(self, name: str) -> None:
        if not name or "/" in name:
            self._respond(400, b"bad file name\n")
            return

        should_fail = STATE.register_request(name, self.path)
        if should_fail:
            self._respond(500, b"simulated failure\n")
            return

        content = (f"content of {name}\n" * CONTENT_MULTIPLIER).encode()

        start = 0
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first = rng[len("bytes="):].split("-", 1)[0]
            if first.isdigit():
                start = int(first)
        if start > len(content):
            start = len(content)
        chunk = content[start:]

        self.send_response(200 if start == 0 else 206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if start > 0:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(content) - 1}/{len(content)}",
            )
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# Server / path helpers
# ---------------------------------------------------------------------------

def start_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_cfd_bat() -> Path | None:
    for c in [
        REPO_ROOT / "cfd" / "cfd.bat",
        REPO_ROOT / "cfd.bat",
        REPO_ROOT / "bin" / "cfd.bat",
    ]:
        if c.is_file():
            return c
    return None


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------

def write_queue(run_dir: Path, lines: list[str]) -> None:
    with (run_dir / ".cfd.queue").open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def run_worker(cfd_path: Path, run_dir: Path, log_path: Path,
               env_extra: dict | None = None, timeout: int = 900) -> tuple[int, float]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    start = time.time()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(
                ["cmd", "/c", str(cfd_path), "__worker__", str(run_dir)],
                cwd=str(run_dir),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                timeout=timeout,
            )
            return proc.returncode, time.time() - start
        except subprocess.TimeoutExpired:
            log.write("\n=== WORKER TIMED OUT ===\n")
            return -1, time.time() - start


def analyze_log(log_path: Path) -> dict:
    if not log_path.exists():
        return {}
    content = log_path.read_text(encoding="utf-8", errors="replace")
    return {
        "retry_lines": content.count("[CFD] Retry "),
        "requeue_lines": content.count("[CFD] Requeued as [E:"),
        "defer_lines": content.count("Retry budget exhausted"),
        "finished_lines": content.count("[CFD] Finished:"),
    }


def run_scenario(cfd_path: Path, name: str, fail_counts: dict[str, int],
                 queue_lines: list[str], base_dir: Path) -> dict:
    run_dir = base_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)

    for fname, count in fail_counts.items():
        STATE.configure(fname, count)

    write_queue(run_dir, queue_lines)

    log_path = run_dir / "worker.log"
    rc, elapsed = run_worker(
        cfd_path, run_dir, log_path,
        env_extra={"CFD_SMALL_DELAY": "0", "CFD_WORKER_EXIT_DELAY": "0"},
    )

    queue_file = run_dir / ".cfd.queue"
    lock_file = run_dir / ".cfd.lock"
    final_queue = (queue_file.read_text(encoding="utf-8", errors="replace")
                   if queue_file.exists() else "")
    final_lock = (lock_file.read_text(encoding="utf-8", errors="replace")
                  if lock_file.exists() else "")

    downloaded = sorted(
        p.name for p in run_dir.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )

    requests = STATE.snapshot()["requests"]

    obs = {
        "scenario": name,
        "configured_fail_counts": fail_counts,
        "initial_queue": queue_lines,
        "worker_returncode": rc,
        "worker_elapsed_seconds": round(elapsed, 2),
        "final_queue_contents": final_queue,
        "final_lock_contents": final_lock,
        "downloaded_files": downloaded,
        "log_markers": analyze_log(log_path),
        "server_request_count": len(requests),
        "server_requests": requests,
    }

    (run_dir / "observations.json").write_text(
        json.dumps(obs, indent=2), encoding="utf-8"
    )
    with (run_dir / "server.log").open("w", encoding="utf-8") as fh:
        for r in requests:
            fh.write(
                f"{r['time']:.3f}  {r['name']:<16} {r['result']:<7} "
                f"(remaining={r['fail_remaining_after']})\n"
            )

    return obs


# ---------------------------------------------------------------------------
# Summary writer
# ---------------------------------------------------------------------------

def write_summary(run_root: Path, all_obs: list[dict]) -> None:
    lines: list[str] = ["CFD fake-server test summary", "=" * 64, ""]

    for obs in all_obs:
        lines.append(f"Scenario: {obs['scenario']}")
        lines.append(f"  configured fail counts: {obs['configured_fail_counts']}")
        lines.append(f"  initial queue: {obs['initial_queue']}")
        lines.append(f"  worker return code: {obs['worker_returncode']}")
        lines.append(f"  worker wall time: {obs['worker_elapsed_seconds']}s")
        lines.append(f"  downloaded files: {obs['downloaded_files']}")
        lines.append(f"  log markers: {obs['log_markers']}")

        per_file: dict[str, dict[str, int]] = {}
        for req in obs["server_requests"]:
            bucket = per_file.setdefault(req["name"], {"fail": 0, "success": 0})
            bucket[req["result"]] += 1
        lines.append("  per-file request counts:")
        if per_file:
            for fname, counts in sorted(per_file.items()):
                lines.append(
                    f"    {fname}: {counts['fail']} fail, {counts['success']} success"
                )
        else:
            lines.append("    (no requests reached the server)")

        lines.append("  final queue contents:")
        if obs["final_queue_contents"].strip():
            for line in obs["final_queue_contents"].splitlines():
                lines.append(f"    {line}")
        else:
            lines.append("    (empty)")

        lines.append(f"  final lock exists: {bool(obs['final_lock_contents'])}")
        lines.append("")

    (run_root / "summary.txt").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def cmd_serve(port: int) -> int:
    start_server(port)
    base = f"http://127.0.0.1:{port}/file"
    cfg = f"http://127.0.0.1:{port}/configure"

    print(f"Fake download server listening on http://127.0.0.1:{port}")
    print()
    print("Configure a file to fail N times before succeeding, e.g.:")
    print(f'  curl "{cfg}?name=item1.bin&fail_count=9"')
    print()
    print("Inspect / reset:")
    print(f"  curl http://127.0.0.1:{port}/state")
    print(f"  curl http://127.0.0.1:{port}/reset")
    print()
    print("Example cfd invocations (run from any test folder):")
    print(f"  cfd curl -fL -C - -O {base}/item1.bin")
    print(f"  cfd {base}/item2.bin")
    print()
    print("Ctrl-C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


def cmd_test() -> int:
    cfd_path = find_cfd_bat()
    if not cfd_path:
        print("ERROR: could not find cfd.bat under cfd/, ./, or bin/")
        return 1
    print(f"Using cfd: {cfd_path}")

    if TESTS_DIR.exists():
        shutil.rmtree(TESTS_DIR)
    TESTS_DIR.mkdir(parents=True)
    run_root = TESTS_DIR / time.strftime("%Y%m%d-%H%M%S")
    run_root.mkdir()

    port = pick_free_port()
    start_server(port)
    base = f"http://127.0.0.1:{port}/file"
    print(f"Fake server: http://127.0.0.1:{port}")

    # Each scenario is (name, fail_counts, initial_queue_lines).
    scenarios = [
        (
            "01-single-success",
            {"item1.bin": 0},
            [f'curl -fL -C - -O "{base}/item1.bin"'],
        ),
        (
            "02-fail-then-succeed",
            {"item1.bin": 3},
            [f'curl -fL -C - -O "{base}/item1.bin"'],
        ),
        (
            # item1 needs 10 fails to exhaust its budget; item2 succeeds
            # immediately. Expect item1 deferred to the lock file and
            # restored to the queue as [E:3] at worker exit.
            "03-exhaust-and-defer",
            {"item1.bin": 10, "item2.bin": 0},
            [
                f'curl -fL -C - -O "{base}/item1.bin"',
                f'curl -fL -C - -O "{base}/item2.bin"',
            ],
        ),
        (
            # Queue pre-seeded with [E:2]. Startup must strip the prefix,
            # so item1 gets a fresh budget: 4 fails on the first pickup
            # then requeue as [E:1], and the 5th attempt succeeds.
            # If the prefix were kept, the item would be deferred after
            # only 3 attempts and never downloaded.
            "04-flag-stripped-on-startup",
            {"item1.bin": 4},
            [f'[E:2]curl -fL -C - -O "{base}/item1.bin"'],
        ),
    ]

    all_obs: list[dict] = []
    for name, fail_counts, queue_lines in scenarios:
        STATE.reset()
        print(f"\n=== {name} ===")
        obs = run_scenario(cfd_path, name, fail_counts, queue_lines, run_root)
        all_obs.append(obs)

        per_file: dict[str, dict[str, int]] = {}
        for req in obs["server_requests"]:
            b = per_file.setdefault(req["name"], {"fail": 0, "success": 0})
            b[req["result"]] += 1

        print(f"  worker rc={obs['worker_returncode']} "
              f"elapsed={obs['worker_elapsed_seconds']}s")
        print(f"  downloaded: {obs['downloaded_files']}")
        for fname, counts in sorted(per_file.items()):
            print(f"    {fname}: {counts['fail']} fail / {counts['success']} ok")
        print("  final queue:")
        for line in obs["final_queue_contents"].splitlines():
            print(f"    {line}")

    write_summary(run_root, all_obs)
    print(f"\nResults written to: {run_root}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fake HTTP download server for testing cfd.bat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", type=int, default=8765, help="port for serve mode")
    ap.add_argument("--test", action="store_true", help="run automated scenarios")
    args = ap.parse_args()

    if args.test:
        return cmd_test()
    return cmd_serve(args.port)


if __name__ == "__main__":
    sys.exit(main())