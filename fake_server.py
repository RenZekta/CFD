#!/usr/bin/env python3
"""
fake_server.py - Fake HTTP download server for exercising cfd retry logic.

Modes:
    python fake_server.py                 # interactive server on port 8765
    python fake_server.py --port 9000     # custom port
    python fake_server.py --test          # automated scenarios -> ./Tests/

Interactive mode prints cfd commands you can paste into a shell.

Server endpoints:
    GET /file/<name>                               serve or fail
    GET /configure?name=<n>&fail_count=<k>         make <n> fail the next k hits
    GET /configure?...&partial_hits=<h>&partial_bytes=<b>
        make the first <h> of those failures truncated transfers that write
        <b> bytes to disk before the connection drops (resumable progress)
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
        # partial_hits[name]: how many of the upcoming failures should be
        # *truncated* transfers (write some bytes, then drop the connection)
        # instead of clean 500s. That is what makes a .part file grow.
        self.partial_hits: dict[str, int] = {}
        self.partial_bytes: dict[str, int] = {}
        # partial_start[name]: 1-based index of the first failing request
        # that is truncated. Lets a scenario make progress *later* in the
        # item's life, not just at the very beginning.
        self.partial_start: dict[str, int] = {}
        self.hit_counts: dict[str, int] = {}
        self.request_log: list[dict] = []

    def configure(self, name: str, fail_count: int,
                  partial_hits: int = 0, partial_bytes: int = 100,
                  partial_start: int = 1) -> None:
        with self.lock:
            self.fail_counts[name] = max(0, int(fail_count))
            self.partial_hits[name] = max(0, int(partial_hits))
            self.partial_bytes[name] = max(0, int(partial_bytes))
            self.partial_start[name] = max(1, int(partial_start))
            self.hit_counts[name] = 0

    def reset(self) -> None:
        with self.lock:
            self.fail_counts.clear()
            self.partial_hits.clear()
            self.partial_bytes.clear()
            self.partial_start.clear()
            self.hit_counts.clear()
            self.request_log.clear()

    def register_request(self, name: str, path: str) -> tuple[bool, int]:
        """Record a request.

        Returns (should_fail, partial_bytes) where partial_bytes > 0 means
        this failure is a truncated download that still writes real data.
        """
        with self.lock:
            remaining = self.fail_counts.get(name, 0)
            should_fail = remaining > 0
            partial = 0
            if should_fail:
                self.fail_counts[name] = remaining - 1
                self.hit_counts[name] = self.hit_counts.get(name, 0) + 1
                hit = self.hit_counts[name]
                start = self.partial_start.get(name, 1)
                if hit >= start and hit < start + self.partial_hits.get(name, 0):
                    partial = self.partial_bytes.get(name, 100)
            self.request_log.append(
                {
                    "time": time.time(),
                    "name": name,
                    "path": path,
                    "result": "fail" if should_fail else "success",
                    "partial_bytes": partial,
                    "fail_remaining_after": self.fail_counts.get(name, 0),
                }
            )
            return should_fail, partial

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
            try:
                hits = int((query.get("partial_hits") or ["0"])[0])
            except ValueError:
                hits = 0
            try:
                nbytes = int((query.get("partial_bytes") or ["100"])[0])
            except ValueError:
                nbytes = 100
            if name:
                STATE.configure(name, count, hits, nbytes)
            self._respond(
                200,
                f"configured {name} fail_count={count} "
                f"partial_hits={hits} partial_bytes={nbytes}\n".encode(),
            )
            return
        if path.startswith("/file/"):
            self._serve_file(path[len("/file/"):])
            return

        self._respond(404, b"not found\n")

    def _serve_file(self, name: str) -> None:
        if not name or "/" in name:
            self._respond(400, b"bad file name\n")
            return

        should_fail, partial = STATE.register_request(name, self.path)
        if should_fail and partial == 0:
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
        # A partial hit advertises the full remaining length but writes only
        # `partial` bytes and then closes, so curl aborts with a non-zero
        # exit code while the bytes it wrote stay on disk - the same shape
        # as "server closed the connection mid-download".
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if start > 0:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(content) - 1}/{len(content)}",
            )
        if partial:
            self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(chunk[:partial] if partial else chunk)
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


def find_launcher() -> Path | None:
    """Locate the cfd entry point for this platform.

    Windows uses cfd.bat (cmd needs a .bat/.cmd on PATH), everywhere else
    cfd.sh. Both are thin launchers that forward to cfd-core.py.
    """
    names = ("cfd.bat", "cfd.cmd") if os.name == "nt" else ("cfd.sh", "cfd")
    for folder in (REPO_ROOT / "cfd", REPO_ROOT, REPO_ROOT / "bin"):
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    return None


def worker_command(launcher: Path) -> list[str]:
    if os.name == "nt":
        return ["cmd", "/c", str(launcher), "__worker__"]
    return ["sh", str(launcher), "__worker__"]


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
                worker_command(cfd_path) + [str(run_dir)],
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
        "progress_reset_lines": content.count("retry counters reset"),
    }


def run_cli(cfd_path: Path, run_dir: Path, args: list[str],
            env_extra: dict | None = None, timeout: int = 900) -> str:
    """Run the launcher as a user would (status/continue/clear/URL).

    CFD_WORKER_MODE=inline makes a spawned worker run in the same process,
    which keeps these tests deterministic (no real console window).
    """
    env = os.environ.copy()
    env["CFD_WORKER_MODE"] = "inline"
    env.setdefault("CFD_SMALL_DELAY", "0")
    env.setdefault("CFD_WORKER_EXIT_DELAY", "0")
    if env_extra:
        env.update(env_extra)

    cmd = (["cmd", "/c", str(cfd_path)] if os.name == "nt"
           else ["sh", str(cfd_path)]) + args
    proc = subprocess.run(
        cmd, cwd=str(run_dir), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=env, timeout=timeout, text=True,
    )
    return proc.stdout


def run_scenario(cfd_path: Path, name: str, fail_counts: dict,
                 queue_lines: list[str], base_dir: Path) -> dict:
    run_dir = base_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)

    for fname, cfg in fail_counts.items():
        if isinstance(cfg, dict):
            STATE.configure(fname, cfg.get("fail_count", 0),
                            cfg.get("partial_hits", 0),
                            cfg.get("partial_bytes", 100))
        else:
            STATE.configure(fname, cfg)

    write_queue(run_dir, queue_lines)

    # The worker log lives NEXT TO the scenario folder, not inside it:
    # the progress check measures the bytes in the download folder, and
    # a log file in there would look like a growing download.
    log_path = base_dir / f"{name}.worker.log"
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
        (p.name, p.stat().st_size) for p in run_dir.iterdir()
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
        "final_lock_exists": lock_file.exists(),
        "downloaded_files": downloaded,
        "log_markers": analyze_log(log_path),
        "server_request_count": len(requests),
        "server_requests": requests,
    }

    (run_dir / "observations.json").write_text(
        json.dumps(obs, indent=2), encoding="utf-8"
    )
    with (base_dir / f"{name}.server.log").open("w", encoding="utf-8") as fh:
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


def cmd_test(impl: Path | None = None) -> int:
    cfd_path = impl if impl else find_launcher()
    if not cfd_path or not cfd_path.is_file():
        print("ERROR: could not find a cfd launcher (cfd.bat / cfd.sh) to test.")
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

    # Each scenario is (name, fail_counts, initial_queue_lines, expect).
    # expect checks the observable outcome so the suite is self-verifying on
    # both Windows and Linux instead of needing a human to read the logs.
    scenarios = [
        (
            "01-single-success",
            {"item1.bin": 0},
            [f'curl -fL -C - -O "{base}/item1.bin"'],
            {"finished": 1, "deferred": 0, "requeued": 0, "resets": 0,
             "queue": [], "files": {"item1.bin": 1344}},
        ),
        (
            "02-fail-then-succeed",
            {"item1.bin": 3},
            [f'curl -fL -C - -O "{base}/item1.bin"'],
            {"finished": 1, "deferred": 0, "requeued": 0, "resets": 0,
             "queue": [], "files": {"item1.bin": 1344}},
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
            {"finished": 1, "deferred": 1, "requeued": 2, "resets": 0,
             "queue": [f'[E:3]curl -fL -C - -O "{base}/item1.bin"'],
             "files": {"item2.bin": 1344}},
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
            {"finished": 1, "deferred": 0, "requeued": 1, "resets": 0,
             "queue": [], "files": {"item1.bin": 1344}},
        ),
        (
            # 12 failures, every one of them a truncated transfer that
            # writes 100 bytes before the connection drops. Because each
            # attempt leaves new bytes on disk the retry counters reset
            # every time, so the item is never deferred: it takes 13
            # attempts (12 partial + 1 clean) and downloads fully.
            # The old logic would have deferred it at attempt 10.
            "05-progress-resets-retries",
            {"item1.bin": {"fail_count": 12, "partial_hits": 12,
                           "partial_bytes": 100}},
            [f'curl -fL -C - -O "{base}/item1.bin"'],
            {"finished": 1, "deferred": 0, "requeued": 0, "resets": 11,
             "queue": [], "files": {"item1.bin": 1344}},
        ),
        (
            # Mixed: 3 attempts make progress, the rest stall. Progress buys
            # the item extra cycles (3 progress + 3 cycles of 3 = 12
            # attempts before deferring) instead of the plain 10.
            "06-progress-then-stall",
            {"item1.bin": {"fail_count": 13, "partial_hits": 3,
                           "partial_bytes": 100}},
            [f'curl -fL -C - -O "{base}/item1.bin"'],
            {"finished": 0, "deferred": 1, "requeued": 2, "resets": 2,
             "queue": [f'[E:3]curl -fL -C - -O "{base}/item1.bin"'],
             "files": {"item1.bin": 300}},
        ),
        (
            # Same idea but with an explicit -o destination: proves the
            # progress check follows the file the command actually writes
            # instead of guessing from the URL.
            "07-explicit-output-name",
            {"item7.bin": {"fail_count": 12, "partial_hits": 12,
                           "partial_bytes": 100}},
            [f'curl -fL -C - -o renamed.bin "{base}/item7.bin"'],
            {"finished": 1, "deferred": 0, "requeued": 0, "resets": 11,
             "queue": [], "files": {"renamed.bin": 1344}},
        ),
        (
            # Two items in one folder. item8 stalls (only its first 3
            # attempts write data) and is deferred; item9 is clean and
            # downloads normally. The progress check watches each item's
            # own file, so item9's 1344-byte file in the same folder never
            # counts as item8 making progress.
            "08-two-items-mixed-progress",
            {"item8.bin": {"fail_count": 13, "partial_hits": 3,
                           "partial_bytes": 100},
             "item9.bin": 0},
            [
                f'curl -fL -C - -O "{base}/item8.bin"',
                f'curl -fL -C - -O "{base}/item9.bin"',
            ],
            {"finished": 1, "deferred": 1, "requeued": 2, "resets": 2,
             "queue": [f'[E:3]curl -fL -C - -O "{base}/item8.bin"'],
             "files": {"item8.bin": 300, "item9.bin": 1344}},
        ),
        (
            # The download-button style URL carries a query string. The
            # name parser must drop "?download=true" and watch item10.bin,
            # not something meaningless - otherwise progress is invisible
            # and a resumable item gets deferred anyway.
            "09-url-with-query-string",
            {"item10.bin": {"fail_count": 12, "partial_hits": 12,
                            "partial_bytes": 100}},
            [f'curl -fL -C - -O "{base}/item10.bin?download=true"'],
            {"finished": 1, "deferred": 0, "requeued": 0, "resets": 11,
             "queue": [], "files": {"item10.bin": 1408}},
        ),
    ]

    all_obs: list[dict] = []
    failures = 0
    for name, fail_counts, queue_lines, expect in scenarios:
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

        markers = obs["log_markers"]
        got = {
            "finished": markers["finished_lines"],
            "deferred": markers["defer_lines"],
            "requeued": markers["requeue_lines"],
            "resets": markers["progress_reset_lines"],
            "queue": obs["final_queue_contents"].splitlines(),
            "files": dict(obs["downloaded_files"]),
        }
        ok = obs["worker_returncode"] == 0
        for key, want in expect.items():
            if got[key] != want:
                ok = False
                print(f"    !! {key}: expected {want!r}, got {got[key]!r}")
        if obs["final_lock_exists"]:
            ok = False
            print("    !! lock file still present at worker exit")
        print(f"  {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures += 1

    cli_scenarios = [
        (
            # A user typing a URL while a worker already holds the folder's
            # claim must not start a second worker - the item just joins the
            # queue. This is the guard that stops two downloads of the same
            # file colliding.
            "10-cli-guard-while-worker-holds-folder",
            {"item11.bin": 0, "item12.bin": 0},
            {".cfd.lock": "cfd worker running\n"},
            [
                [f"{base}/item11.bin"],
                [f"{base}/item12.bin"],
                ["status"],
            ],
            [
                ["A download is already running in this folder.",
                 "Added to queue at position 1:"],
                ["A download is already running in this folder.",
                 "Added to queue at position 2:"],
                ["A download worker is active in this folder.",
                 "1. curl -fL -C - -O \"http://127.0.0.1:",
                 "2. curl -fL -C - -O \"http://127.0.0.1:"],
            ],
        ),
        (
            # Fully drained retries are not lost: they sit in the queue, and
            # `cfd continue` restarts the worker over them.
            "11-cli-continue-after-drained-retries",
            {"item13.bin": 1},
            {".cfd.queue":
                 '[E:3]curl -fL -C - -O "{base}/item13.bin"\n'.format(base=base)},
            [["status"], ["continue"], ["status"]],
            [
                ["No active worker in this folder.",
                 "[E:3]curl -fL -C -"],
                ["Resuming queued downloads for this folder...",
                 "[CFD] Finished:"],
                ["No active worker in this folder.", "Queue is empty."],
            ],
        ),
        (
            # `cfd clear` wipes the queue and the in-progress marker, and the
            # next `cfd <url>` starts from a clean slate.
            "12-cli-clear",
            {"item14.bin": 0},
            {".cfd.queue":
                 'curl -fL -C - -O "{base}/item14.bin"\n'.format(base=base)},
            [["clear"], ["status"], [f"{base}/item14.bin"], ["status"]],
            [
                ["Pending queue and in-progress marker cleared for this folder."],
                ["No active worker in this folder.", "Queue is empty."],
                ["Queued (position 1):", "[CFD] Finished:"],
                ["No active worker in this folder.", "Queue is empty."],
            ],
        ),
    ]

    for name, fail_counts, seed_files, steps, expects in cli_scenarios:
        STATE.reset()
        run_dir = run_root / name
        run_dir.mkdir(parents=True, exist_ok=True)
        for fname, cfg in fail_counts.items():
            STATE.configure(fname, cfg)
        for fname, content in seed_files.items():
            (run_dir / fname).write_text(content, encoding="utf-8")

        print(f"\n=== {name} ===")
        outputs = []
        ok = True
        for args, expect in zip(steps, expects):
            out = run_cli(cfd_path, run_dir, args)
            outputs.append(out)
            print("  $ cfd " + " ".join(args))
            for line in out.splitlines():
                print("    " + line)
            for needle in expect:
                if needle not in out:
                    ok = False
                    print(f"    !! missing expected output: {needle!r}")
        print(f"  {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures += 1

        all_obs.append({
            "scenario": name,
            "configured_fail_counts": fail_counts,
            "initial_queue": seed_files.get(".cfd.queue", "").splitlines(),
            "worker_returncode": 0 if ok else 1,
            "worker_elapsed_seconds": 0,
            "final_queue_contents": ((run_dir / ".cfd.queue").read_text(encoding="utf-8", errors="replace")
                                     if (run_dir / ".cfd.queue").exists() else ""),
            "final_lock_contents": ((run_dir / ".cfd.lock").read_text(encoding="utf-8", errors="replace")
                                    if (run_dir / ".cfd.lock").exists() else ""),
            "downloaded_files": sorted(
                (p.name, p.stat().st_size) for p in run_dir.iterdir()
                if p.is_file() and not p.name.startswith(".")
            ),
            "log_markers": {"cli_outputs": outputs},
            "server_request_count": len(STATE.snapshot()["requests"]),
            "server_requests": STATE.snapshot()["requests"],
        })

    write_summary(run_root, all_obs)

    total = len(scenarios) + len(cli_scenarios)
    print(f"\n{total - failures}/{total} scenarios passed.")
    print(f"Results written to: {run_root}")
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fake HTTP download server for testing cfd.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", type=int, default=8765, help="port for serve mode")
    ap.add_argument("--test", action="store_true", help="run automated scenarios")
    ap.add_argument("--impl", help="path to a specific cfd launcher to test "
                                   "(default: cfd.bat on Windows, cfd.sh elsewhere)")
    args = ap.parse_args()

    if args.test:
        return cmd_test(Path(args.impl) if args.impl else None)
    return cmd_serve(args.port)


if __name__ == "__main__":
    sys.exit(main())