#!/usr/bin/env python3
"""
cfd-core.py - the whole cfd app, platform-neutral.

cfd.bat (Windows) and cfd.sh (Linux/WSL/macOS) are thin launchers: they
find Python 3, set CFD_LAUNCHER to their own path, and hand every argument
to this file. The launcher exists only because Windows PATH needs a .bat
and Linux needs an executable shebang - the logic lives here, once, so the
two platforms cannot drift apart.

Behaviour mirrors the original cfd.bat exactly: the same .cfd.queue /
.cfd.lock / .cfd.current files, the same [E:n] big-cycle bookkeeping, the
same messages and the same exit codes.

The lock file is a claim on the folder, not a liveness probe. It is only
removed when the worker finishes normally or when the user runs
`cfd continue`. Never delete .cfd.lock without flushing its deferred lines
back to the queue - line 2+ of the lock IS the deferred list.

Env overrides:
  CFD_SMALL_DELAY        seconds between retries (default 10)
  CFD_WORKER_EXIT_DELAY  seconds to linger after the queue empties (default 5)
  CFD_TERMINAL           Linux only: command used to open the worker window
  CFD_WORKER_MODE        window | background | inline (default: window)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SEP = "=" * 51
BAR = "-" * 51

LOCK_NAME = ".cfd.lock"
QUEUE_NAME = ".cfd.queue"
CURRENT_NAME = ".cfd.current"
LOG_NAME = ".cfd.log"
CACHE_NAME = ".cache"

SMALL_RETRIES = 3
BIG_RETRIES = 3

# A model split with gguf-split is name-00001-of-00004.gguf ... name-00004-of-00004.gguf.
SHARD_RE = re.compile(r"-(\d{5})(-of-)(\d{5})(?=\.gguf)", re.IGNORECASE)
MAX_SHARDS = 500

# Terminals worth trying on Linux, in preference order. Each entry is
# (program, argv prefix) where the prefix takes a single shell command.
TERMINALS = (
    ("tmux", ["tmux", "new", "-d", "-s", "cfd-worker"]),
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("konsole", ["konsole", "-e"]),
    ("alacritty", ["alacritty", "-e"]),
    ("kitty", ["kitty", "--no-parent"]),
    ("xfce4-terminal", ["xfce4-terminal", "-e"]),
    ("mate-terminal", ["mate-terminal", "-e"]),
    ("xterm", ["xterm", "-e"]),
)


# ---------------------------------------------------------------------------
# Bookkeeping files
# ---------------------------------------------------------------------------
def paths(wdir: str) -> tuple[Path, Path, Path]:
    base = Path(wdir)
    return base / LOCK_NAME, base / QUEUE_NAME, base / CURRENT_NAME


def read_lines(path: Path) -> list[str]:
    """Read a bookkeeping file, dropping blank lines like cmd's `for /f`
    does, so a stray empty line never becomes a download."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return [line.rstrip("\r\n") for line in fh if line.strip()]


def write_lines(path: Path, lines: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Parsing the pasted command
# ---------------------------------------------------------------------------
def join_args(argv: list[str]) -> str:
    """Rebuild the command the user typed, exactly as the shell saw it.

    cmd's %* behaves the same way: whatever quoting the user typed is kept
    verbatim, so a path with a space still survives as one token if the user
    quoted it.
    """
    return " ".join(argv)


def looks_like_hf(command: str) -> bool:
    return "hf download" in command.lower()


def looks_like_url(command: str) -> bool:
    low = command.strip().lower()
    return low.startswith("http://") or low.startswith("https://")


def url_to_curl(command: str) -> str:
    """A plain download-button URL becomes a curl command. The query
    string is dropped (…?download=true) and -f makes curl exit non-zero on
    4xx/5xx, so the retry loop can see the failure instead of curl writing
    the error body to disk as if it were content."""
    url = command.strip().split("?", 1)[0]
    return 'curl -fL -C - -O "{0}"'.format(url)


def other_shards(command: str) -> list[str]:
    """The commands for shards 2..N when `command` downloads shard 1 of a
    split model, so adding the first shard queues the whole model. Any other
    command, including a later shard added on purpose, gives [].

    Each command is the original with only the shard number changed, so it
    works for hf and URL commands alike.
    """
    shards = {(m.group(1), m.group(3)) for m in SHARD_RE.finditer(command)}
    if len(shards) != 1:
        return []
    index, total = next(iter(shards))
    count = int(total)
    if int(index) != 1 or count < 2:
        return []
    if count > MAX_SHARDS:
        print("[CFD] Shard 1 of {0}: too many shards to queue automatically.".format(count))
        return []
    return [
        SHARD_RE.sub(lambda m, n=n: "-{0:05d}{1}{2}".format(n, m.group(2), m.group(3)), command)
        for n in range(2, count + 1)
    ]


def command_key(line: str) -> str:
    """What makes two queue entries the same download: the command itself,
    without an [E:n] prefix or differences in spacing."""
    return " ".join(parse_item(line)[2].split())


def pending_commands(wdir: str) -> dict[str, str]:
    """Every command already in this folder's pipeline, mapped to where it
    is: the one downloading now, the queued ones, and the ones a worker has
    parked in the lock file."""
    lock, queue, current = paths(wdir)
    found: dict[str, str] = {}
    for line in read_lines(current):
        found[command_key(line)] = "downloading"
    for line in read_lines(queue) + read_lines(lock)[1:]:
        found.setdefault(command_key(line), "in the queue")
    return found


def run_command(command: str, wdir: str) -> int:
    """Run one download attempt and return its exit code.

    The command goes through the platform shell because cfd accepts
    whatever the user pastes - the same contract the batch version had.
    """
    if looks_like_hf(command):
        command = command + ' --local-dir "{0}"'.format(wdir)
    try:
        return subprocess.run(command, shell=True).returncode
    except OSError as exc:
        print("[CFD] Could not run command: {0}".format(exc))
        return 1


# ---------------------------------------------------------------------------
# Progress measurement
# ---------------------------------------------------------------------------
def derive_target(command: str) -> str:
    """Name of the exact file this command writes.

      - curl -o <file>  -> that file
      - curl -O <url>   -> basename of the URL, query string dropped
      - hf download ... -> the file named in the command
    Returns "" when the command names no file at all.
    """
    target = ""
    match = re.search(r'(?:--output|-o)(?:=|\s+)"?([^\s"]+)', command)
    if match:
        target = match.group(1)
    else:
        for token in command.split():
            piece = token.strip('"').split("?")[0]
            if not target and "://" in piece:
                target = piece.split("/")[-1]
            elif not target and re.fullmatch(r"[^-][^\"]*\.[A-Za-z0-9]+", piece):
                target = piece
    return target


def measure_disk(wdir: str, command: str) -> tuple[int, str]:
    """Return (bytes, target) for the file this command downloads.

    Counted in Python, which is 64-bit everywhere - cmd's `set /a` and
    `if ... gtr` cap at 2^31-1, so a 3.5 GB .part file compares wrong there.
    """
    base = Path(wdir)
    target = derive_target(command)

    if target:
        leaf = re.split(r"[\\/]", target)[-1]
        best = None
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name != leaf:
                    continue
                try:
                    size = os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                if best is None or size > best:
                    best = size
        if best is not None:
            return best, leaf

        # The exact file is not there yet: hf download keeps partial data
        # in .cache until the file is finalised.
        total = 0
        cache = base / CACHE_NAME
        if cache.exists():
            for root, _dirs, files in os.walk(cache):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        return total, leaf

    total = 0
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name.startswith(".cfd"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total, "folder"


def check_progress(state: dict) -> None:
    """Compare the bytes on disk against the previous attempt of this item.

    Sets state["prev_bytes"], state["attempt_progress"], state["attempt_target"].
    """
    bytes_now, target = measure_disk(state["wdir"], state["next_cmd"])
    state["attempt_progress"] = bytes_now > state["prev_bytes"]
    state["prev_bytes"] = bytes_now
    state["attempt_target"] = target


def attempt_once(state: dict) -> int:
    state["attempt_exit"] = run_command(state["next_cmd"], state["wdir"])
    check_progress(state)
    return state["attempt_exit"]


# ---------------------------------------------------------------------------
# The lock: a claim on the folder
# ---------------------------------------------------------------------------
def try_claim_folder(wdir: str) -> bool:
    """Atomically create the lock. True means this invocation claimed the
    folder, False means a worker already owns it.

    open(path, "x") makes check-and-claim a single operation, so two `cfd`
    commands fired in the same instant cannot both start a worker.
    """
    lock, _queue, _current = paths(wdir)
    try:
        with lock.open("x", encoding="utf-8") as fh:
            fh.write("cfd worker running\n")
        return True
    except OSError:
        return False


def release_lock(wdir: str) -> None:
    """Delete the lock, after flushing its deferred lines back to the queue.

    Deferred items live on lock lines 2+; dropping the lock without flushing
    would silently lose downloads.
    """
    lock, queue, _current = paths(wdir)
    deferred = read_lines(lock)[1:]
    for line in deferred:
        append_line(queue, line)
    remove_file(lock)


def _spawn_background_worker(wdir: str, core: str) -> None:
    """Detached worker: output goes to .cfd.log in the download folder."""
    log_path = Path(wdir) / LOG_NAME
    # Line-buffered so `cfd log` / `tail -f` show progress as it happens
    # instead of after a 8 KB block fills up.
    with log_path.open("w", encoding="utf-8", buffering=1) as log:
        subprocess.Popen(
            [sys.executable, core, "__worker__", wdir],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print("[CFD] Worker started in the background (no terminal window available).")
    print("[CFD] Watch it with:  tail -f {0}".format(log_path))
    print("[CFD] Or run:  cfd log")


def spawn_worker(wdir: str) -> None:
    """Start the worker as a separate, user-visible process.

    Windows: a new console window titled "CFD Worker", launched through
    cfd.bat so the PATH entry and window title stay exactly as before.
    Linux: a terminal window if one is available, otherwise a detached
    background process whose output lands in .cfd.log.
    """
    mode = os.environ.get("CFD_WORKER_MODE", "window")
    if mode == "inline":
        worker_loop(wdir)
        return

    core = os.path.abspath(__file__)

    if os.name == "nt":
        launcher = os.environ.get("CFD_LAUNCHER") or core
        if launcher.lower().endswith((".bat", ".cmd")):
            cmd = ["cmd", "/c", launcher, "__worker__", wdir]
        else:
            cmd = [sys.executable, launcher, "__worker__", wdir]
        try:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError:
            _spawn_background_worker(wdir, core)
        return

    forced = os.environ.get("CFD_TERMINAL")
    candidates = [] if forced in ("none", "background", "log") else (
        [(forced, [forced])] if forced else list(TERMINALS)
    )
    script = '{0} "{1}" __worker__ "{2}"'.format(sys.executable, core, wdir)

    for name, prefix in candidates:
        if not shutil.which(name):
            continue
        try:
            if name == "tmux":
                subprocess.Popen(
                    ["tmux", "new-session", "-d", "-s", "cfd-worker", script]
                )
                print("[CFD] Worker started in a detached tmux session 'cfd-worker'.")
                print("[CFD] Watch it with:  tmux attach -t cfd-worker")
            elif name == "gnome-terminal":
                subprocess.Popen(
                    prefix + ["--working-directory", wdir, "sh", "-c",
                              script + "; read -p 'Done. Press Enter to close.'"]
                )
            elif name == "konsole":
                subprocess.Popen(prefix + ["--cd", wdir, "-e", "sh", "-c",
                                          script + "; read"])
            elif name == "alacritty":
                subprocess.Popen(prefix + ["--workdir", wdir, "-e", "sh", "-c",
                                          script + "; read"])
            elif name == "kitty":
                subprocess.Popen(prefix + ["--working-directory", wdir,
                                          "sh", "-c", script + "; read"])
            else:
                subprocess.Popen(prefix + ["sh", "-c", script + "; read"])
            return
        except OSError:
            continue

    _spawn_background_worker(wdir, core)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def strip_error_flags(queue: Path) -> None:
    """Remove all [E:n] prefixes so every item gets a fresh set of
    big-cycle retries for this worker session."""
    if not queue.exists():
        return
    stripped = []
    for line in read_lines(queue):
        stripped.append(line[5:] if line[:3] == "[E:" else line)
    write_lines(queue, stripped)


def parse_item(line: str) -> tuple[bool, int, str]:
    """Split an "[E:n]<command>" line into (errored, completed_cycles, command)."""
    if line[:3] == "[E:":
        digit = line[3:4]
        return True, int(digit) if digit.isdigit() else 0, line[5:]
    return False, 0, line


def worker_loop(wdir: str) -> None:
    wdir = os.path.abspath(wdir)
    lock, queue, current = paths(wdir)

    small_delay = int(os.environ.get("CFD_SMALL_DELAY", "10"))
    exit_delay = int(os.environ.get("CFD_WORKER_EXIT_DELAY", "5"))

    # The worker owns the folder. The claim normally already exists (the
    # launcher created it atomically); create it when run directly.
    if not lock.exists():
        try_claim_folder(wdir)

    print(SEP)
    print("CFD Worker")
    print("Folder: {0}".format(wdir))
    print(SEP)

    # Strip all [E:n] prefixes so every item gets a fresh set of big-cycle
    # retries for this worker session.
    strip_error_flags(queue)

    while True:
        queue_lines = read_lines(queue)
        if not queue_lines:
            break

        next_line = queue_lines[0]
        write_lines(queue, queue_lines[1:])

        errored, big_count, next_cmd = parse_item(next_line)

        # Defensive: an [E:n] item already at the cycle limit should not be
        # retried in this session - defer it straight away.
        if errored and big_count >= BIG_RETRIES:
            append_line(lock, "[E:{0}]{1}".format(big_count, next_cmd))
            print("[CFD] Session retry budget already exhausted. Deferring: {0}".format(next_cmd))
            continue

        # Mark this one as "currently downloading" in case of a crash.
        write_lines(current, [next_cmd])

        print(BAR)
        print("[CFD] Downloading: {0}".format(next_cmd))
        print(BAR)

        # Baseline for the progress check: every attempt of this item is
        # compared against the byte count the previous attempt left behind.
        state = {
            "wdir": wdir,
            "next_cmd": next_cmd,
            "prev_bytes": 0,
            "attempt_progress": False,
            "attempt_target": "folder",
            "attempt_exit": 1,
        }
        check_progress(state)

        # ---- Initial attempt (only on the first pickup of an item) ----
        initial_ok = False
        if not errored:
            if attempt_once(state) == 0:
                initial_ok = True

        if initial_ok:
            finish_item(wdir, current, next_cmd)
            continue

        # ---- Retry loop: up to SMALL_RETRIES retries with delay ----
        retry = 0
        while retry < SMALL_RETRIES:
            retry += 1
            print("[CFD] Retry {0} of {1}. Waiting {2}s...".format(retry, SMALL_RETRIES, small_delay))
            if small_delay > 0:
                time.sleep(small_delay)

            if attempt_once(state) == 0:
                break

            # Progress beats retries: if this attempt wrote new bytes the
            # download is resumable and moving forward, so the item is
            # treated as brand new - no retries used, no big cycles counted.
            if state["attempt_progress"]:
                retry = 0
                big_count = 0
                errored = False
                print("[CFD] {0} grew since the last attempt - retry counters reset.".format(
                    state["attempt_target"]))

        if state["attempt_exit"] == 0:
            finish_item(wdir, current, next_cmd)
            continue

        # ---- A big cycle just completed for this item ----
        new_cycle = 1 if not errored else big_count + 1
        if new_cycle >= BIG_RETRIES:
            append_line(lock, "[E:{0}]{1}".format(new_cycle, next_cmd))
            print("[CFD] Retry budget exhausted for this session. Deferring: {0}".format(next_cmd))
        else:
            append_line(queue, "[E:{0}]{1}".format(new_cycle, next_cmd))
            print("[CFD] Requeued as [E:{0}]: {1}".format(new_cycle, next_cmd))
        remove_file(current)

    release_lock(wdir)

    print(SEP)
    print("[CFD] Queue empty - all downloads for this folder are complete.")
    print(SEP)
    if exit_delay > 0:
        time.sleep(exit_delay)


def finish_item(wdir: str, current: Path, next_cmd: str) -> None:
    """A successful download discards any errored state."""
    cache = Path(wdir) / CACHE_NAME
    if cache.exists():
        print("Cleaning up .cache folder...")
        shutil.rmtree(cache, ignore_errors=True)
    remove_file(current)
    print("[CFD] Finished: {0}".format(next_cmd))


# ---------------------------------------------------------------------------
# Helper commands
# ---------------------------------------------------------------------------
def cmd_status() -> None:
    wdir = os.getcwd()
    lock, queue, current = paths(wdir)

    if lock.exists():
        print("[CFD] A download worker is active in this folder.")
    else:
        print("[CFD] No active worker in this folder.")

    for line in read_lines(current):
        print("[CFD] In progress: {0}".format(line))

    queue_lines = read_lines(queue)
    if queue_lines:
        print("[CFD] Pending items in queue:")
        for index, line in enumerate(queue_lines, start=1):
            print("{0}. {1}".format(index, line))
    else:
        print("[CFD] Queue is empty.")


def cmd_clear() -> None:
    wdir = os.getcwd()
    _lock, queue, current = paths(wdir)
    found = False

    if queue.exists():
        remove_file(queue)
        found = True
    if current.exists():
        remove_file(current)
        found = True

    if found:
        print("[CFD] Pending queue and in-progress marker cleared for this folder.")
        print("[CFD] Note: any partially downloaded .cache folder is left untouched.")
    else:
        print("[CFD] Queue was already empty.")


def cmd_continue() -> None:
    wdir = os.getcwd()
    lock, queue, current = paths(wdir)

    # A stale lock can carry session-deferred items from the previous
    # session. Flush them back to the queue before clearing the claim, so
    # a fresh worker starts them again with a full retry budget.
    if lock.exists():
        release_lock(wdir)

    current_lines = read_lines(current)
    current_cmd = current_lines[0] if current_lines else ""
    queue_lines = read_lines(queue)

    if not current_cmd and not queue_lines:
        print("[CFD] Nothing to continue - no saved queue or in-progress download found here.")
        return

    if current_cmd:
        write_lines(queue, [current_cmd] + queue_lines)
        remove_file(current)
        print("[CFD] Resuming interrupted download (existing .cache will be reused/verified): {0}".format(
            current_cmd))
    else:
        print("[CFD] Resuming queued downloads for this folder...")

    spawn_worker(wdir)


def cmd_log() -> None:
    log_path = Path(os.getcwd()) / LOG_NAME
    if not log_path.exists():
        print("[CFD] No worker log in this folder yet.")
        print("[CFD] On Windows the worker runs in its own window, so there is nothing here to show.")
        return
    for line in read_lines(log_path)[-40:]:
        print(line)


def cmd_uninstall() -> None:
    launcher = Path(os.environ.get("CFD_LAUNCHER") or __file__)
    install_dir = launcher.parent

    print(SEP)
    print("CFD - Uninstalling")
    print(SEP)
    print("Install folder: {0}".format(install_dir))
    print()

    if os.name == "nt":
        import winreg

        removed = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
                try:
                    current = winreg.QueryValue(key, "Path")
                except OSError:
                    current = ""
                parts = [p for p in str(current).split(";") if p]
                kept = [p for p in parts
                        if p.rstrip("\\").lower() != str(install_dir).rstrip("\\").lower()]
                if len(kept) != len(parts):
                    winreg.setValue(key, "Path", ";".join(kept))
                    removed = True
        except OSError as exc:
            print("Could not edit PATH: {0}".format(exc))

        if removed:
            print("Removed {0} from PATH.".format(install_dir))
        else:
            print("{0} was not in PATH.".format(install_dir))
    else:
        print("To finish removing cfd, drop {0} from your PATH if you added it".format(install_dir))
        print("in a shell profile (~/.bashrc, ~/.zshrc, ~/.profile).")

    print("Removing installed files...")
    leftovers = [name for name in ("cfd", "cfd.bat", "cfd.cmd", "cfd.sh",
                                   "cfd-core.py", "install.bat", "install.sh")
                 if (install_dir / name).exists()]
    for name in leftovers:
        remove_file(install_dir / name)
    try:
        install_dir.rmdir()
    except OSError:
        if install_dir.exists():
            if any(install_dir.iterdir()):
                print("Install folder is not empty: {0}".format(install_dir))
            else:
                print("The install folder is empty but could not be removed"
                      " (you may be standing in it). Delete it by hand:")
                print("  rmdir \"{0}\"".format(install_dir))

    print()
    print(SEP)
    print("Done. cfd has been uninstalled.")
    print("Close and reopen any terminal windows for the change to take effect.")
    print(SEP)


def usage() -> None:
    print(SEP)
    print("CFD - Current Folder Downloader")
    print(SEP)
    print("Usage:")
    print("  cfd hf download <hf-url>")
    print("  cfd <direct-https-url>")
    print("    (adding shard 1 of a split model, name-00001-of-0000N.gguf, queues all its shards)")
    print("  cfd status          - show active/current/queued downloads here")
    print("  cfd continue / c    - resume after a crash/interruption")
    print("  cfd clear           - wipe the pending queue here")
    print("  cfd log             - show the tail of the worker log (background mode)")
    print("  cfd uninstall       - remove cfd")
    print()
    print("Examples:")
    print("  cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf")
    print("  cfd https://huggingface.co/prism-ml/Bonsai-27B-gguf/resolve/main/Bonsai-27B-Q1_0.gguf?download=true")


def cmd_add(command: str) -> None:
    target_dir = os.getcwd()

    is_hf = looks_like_hf(command)
    is_url = looks_like_url(command)

    if not is_hf and not is_url:
        print("[Error] Invalid command format.")
        print('Paste either an "hf download ..." command, or a direct download')
        print('URL (e.g. the "download" button link from a huggingface.co file page).')
        sys.exit(1)

    if not is_hf and is_url:
        command = url_to_curl(command)
        print("[CFD] Parsed as direct URL - will run: {0}".format(command))

    # Adding shard 1 of a split model queues the rest of it too.
    shards = other_shards(command)
    if shards:
        print("[CFD] Split model: shard 1 of {0}. Also queueing shards 2-{0}.".format(len(shards) + 1))

    lock, queue, _current = paths(target_dir)

    # A command that is already downloading or waiting (say a shard added by
    # hand earlier) is not queued a second time.
    known = pending_commands(target_dir)
    added = []
    for item in [command] + shards:
        key = command_key(item)
        if key in known:
            print("[CFD] Already {0}, skipped: {1}".format(known[key], item))
            continue
        known[key] = "in the queue"
        append_line(queue, item)
        added.append(item)
    first_position = len(read_lines(queue)) - len(added) + 1

    running = lock.exists()
    claimed = False
    # Nothing new may still leave a queue with no worker behind it.
    if not running and (added or read_lines(queue)):
        claimed = try_claim_folder(target_dir)
        running = not claimed

    if added:
        if running:
            print("[CFD] A download is already running in this folder.")
            print("[CFD] Added to queue at position {0}: {1}".format(first_position, added[0]))
            print("[CFD] It will start automatically once earlier downloads finish.")
        else:
            print("[CFD] Queued (position {0}): {1}".format(first_position, added[0]))
        for offset, item in enumerate(added[1:], start=1):
            print("[CFD] Added to queue at position {0}: {1}".format(first_position + offset, item))
    else:
        print("[CFD] Nothing new to queue.")

    if claimed:
        print("[CFD] Opening download worker window...")
        spawn_worker(target_dir)


def main(argv: list[str]) -> int:
    if not argv or not join_args(argv).strip():
        usage()
        return 1

    first = argv[0].lower()
    if first == "uninstall":
        cmd_uninstall()
        return 0
    if first in ("status", "queue"):
        cmd_status()
        return 0
    if first == "clear":
        cmd_clear()
        return 0
    if first in ("continue", "c"):
        cmd_continue()
        return 0
    if first == "log":
        cmd_log()
        return 0
    if first == "__worker__":
        worker_loop(argv[1] if len(argv) > 1 else os.getcwd())
        return 0

    cmd_add(join_args(argv))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n[CFD] Interrupted.")
        sys.exit(130)
