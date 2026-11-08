# CFD — Current Folder Downloader

A tiny CLI tool that makes downloading from Hugging Face as easy as `npm install`.

Type `cfd` and paste your `hf download` command in any folder, and CFD calls HF to download straight into that folder. Works on Windows and on Linux (any POSIX shell); the download logic is identical on both.

Warning! Until the [issue](https://github.com/huggingface/huggingface_hub/issues/4196#issuecomment-6607510350) is open, don't use hf for downloads, it doesn't resume downloads. Use download links with CFD, they download and resume reliably.
## Features

- **Call from anywhere** — install once, then run `cfd` from any folder, just like `npm` or `git`.
- **Downloads to your current folder** — no more moving a the downloaded files around drives.
- **Background download queue** — paste a second (or third, or tenth) `cfd` + `hf download` command while the first is still running. Each one queues up and starts automatically once the folder is free.
- **Per-folder queues** — downloads to different folders run in parallel; only downloads *into the same folder* wait their turn (since `hf download` can only use one `.cache` at a time).
- **Crash recovery** — if your PC crashes or the worker window gets closed mid-download, `cfd continue` / `cfd c` picks up right where it left off.
- **Self-installing** — adds itself to your PATH, cleans up any old copies, and can uninstall itself just as easily.

## Requirements

- Python 3 — all the logic lives in `cfd-core.py`; `cfd.bat` / `cfd.sh` are just thin launchers
- `curl` — shipped with Windows 10/11 and present on most Linux systems
- [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli) on your PATH, if you want to paste `hf download ...` commands:
  ```
  pip install -U "huggingface_hub[cli]"
  ```

## Installation

**Windows**

1. Put `cfd.bat`, `cfd-core.py` and `install.bat` in the same folder (or `git clone` the repo).
2. Double-click `install.bat` (or run it from a terminal).
3. Open a new Terminal window to use cfd.

`install.bat`:
- Copies `cfd.bat` **and** `cfd-core.py` to `%LOCALAPPDATA%\CFD`
- Adds that folder to your user `PATH` (no admin rights needed)
- Finds and removes any stray `cfd.bat` elsewhere on your `PATH` so there's no conflict
- Re-running it later safely overwrites the existing install with a newer version

**Linux**

1. Put `cfd.sh`, `cfd-core.py` and `install.sh` in the same folder (or `git clone` the repo)..
2. Run `install.sh` as a Program
3. Open a new Terminal window to use cfd.

`install.sh` copies the launcher to `~/.local/bin/cfd` (override with `CFD_INSTALL_DIR=/your/path sh install.sh`), makes it executable, and exports that folder in your shell profile if it is not already on `PATH`.

## Usage

Open a terminal in whatever folder you want the files to land in, type `cfd` and paste the "Download with hf CLI" command from the [Hugging Face website](https://huggingface.co). For example:

```
cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf
```

A **CFD Worker** window opens and starts downloading into your current folder. Your terminal is free immediately — `cd` into another folder or paste another `cfd` command right away.

On Linux the worker opens in `tmux`, `gnome-terminal`, `konsole`, `alacritty`, `kitty`, `xfce4-terminal`, `mate-terminal` or `xterm`, whichever is installed. If none is available (SSH session, headless box), it runs detached and writes its output to `.cfd.log` in the download folder — read it with `cfd log` or `tail -f .cfd.log`. Force a choice with `CFD_TERMINAL=<command>`, or skip the window entirely with `CFD_WORKER_MODE=background`.

If you paste another cfd download command **for the same folder** while one is already running, it is added to that folder's download queue:

```
cfd hf download hf://another-org/another-model
[CFD] A download is already running in this folder.
[CFD] Added to queue at position 2: hf download hf://another-org/another-model
```

Once the current download finishes (and its temporary `.cache` folder is cleaned up), the worker automatically starts the next queued item — repeating until the queue is empty, at which point the worker window closes itself.

## Retries and resumable downloads

Each queued item gets a retry budget per worker session:

- **First pickup:** 1 attempt + 3 quick retries (with `CFD_SMALL_DELAY` seconds between them, 10s by default).
- **After that:** the item is requeued at the tail with an `[E:n]` marker, where `n` counts the completed retry cycles. Each further cycle is 3 attempts.
- **Three cycles exhausted** (10 attempts total) → the item is parked in the lock file and put back on the queue as `[E:3]` when the worker exits, so a later session tries it fresh. Startup also strips every `[E:n]` marker, so each session starts clean.

**Progress resets the budget.** Before and after every attempt the worker measures the file that command is writing to — the `-o` destination if given, otherwise the basename of the URL (query string dropped), searched for recursively so a partial in a subfolder still counts. If an attempt wrote new bytes to **that file** — a resumable download that got partway before the connection died (`curl: (56) ... server closed abruptly` is the classic case) — the item is treated as brand new: its short retry counter and its `[E:n]` cycle counter are both reset to 0. Only attempts that wrote *nothing* count against the budget, so a download that keeps making headway never gets deferred. For `hf download`, whose partial data sits in `.cache` until the file is finalised, the check falls back to the bytes in `.cache`; if no file name can be worked out at all, it uses the folder total. (The batch version did this counting in PowerShell because cmd's arithmetic caps at 2^31-1, which a multi-GB `.part` file blows straight through; in the Python core sizes are native 64-bit integers.)

## Commands

| Command | Alias | Description |
|---|---|---|
| `cfd hf download <link>` | | Downloads to the current folder, or queues if a download is already running here |
| `cfd status` | `cfd queue` | Shows whether a worker is active, what is currently downloading, and what is queued |
| `cfd continue` | `cfd c` | Resumes downloads after a crash, or restarts a queue whose retry budget ran out |
| `cfd clear` | | Wipes the pending queue for the current folder |
| `cfd log` | | Shows the tail of `.cfd.log` (Linux background mode) |
| `cfd uninstall` | | Removes `cfd` from PATH and deletes its installed files |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CFD_SMALL_DELAY` | 10 | Seconds between retries inside a cycle |
| `CFD_WORKER_EXIT_DELAY` | 5 | Seconds the worker lingers after the queue empties |
| `CFD_TERMINAL` | auto | Which terminal program to open the worker in (Linux); `none` forces the detached/log mode |
| `CFD_WORKER_MODE` | `window` | `window`, `background`, or `inline` (run the worker in the current process — used by the test suite) |

## The lock, crashes, and `cfd continue`

If your PC crashes or you force-close a CFD Worker window mid-download, reopen the same folder and run:

```
cfd c
```

This clears the leftover lock and re-queues whatever was in progress, ahead of anything still waiting. `hf download` handles the rest — it checks the existing `.cache` against what is already on disk and resumes/verifies rather than starting over.

The lock is a **claim on the folder**, not a heartbeat: CFD never deletes it on its own, so a stale lock from a killed worker still blocks a second worker from starting there. `cfd continue` is the only thing that clears it — which is also how you restart a queue whose retries have all been used up. Deferred items live on the lock file's extra lines, so releasing the lock always flushes them back onto the queue first; losing that flush would silently lose downloads.

## Uninstalling

```
cfd uninstall
```

This removes the install folder from your `PATH` (Windows edits the user `PATH` registry entry; on Linux it deletes the installed files and tells you which profile line to drop) and deletes the installed files. Close and reopen your terminal afterward.

## How it works

`cfd` is a thin launcher (`cfd.bat` on Windows, `cfd.sh` on Linux) that finds Python 3 and hands every argument to `cfd-core.py`, which holds all the logic. When you run a download command, it:

1. Recognises either an `hf download ...` command or a bare `https://` URL (which it turns into a `curl -fL -C - -O` command)
2. Appends it to a per-folder queue file (`.cfd.queue`)
3. If no worker already owns that folder, claims it atomically (`.cfd.lock`) and spawns a **CFD Worker** window that processes the queue

The worker pulls one command at a time from the queue, writes it to `.cfd.current` while it is running (so a crash leaves behind a record of exactly what was interrupted), runs it with `--local-dir` pointed at the target folder, and cleans up the `.cache` folder on success. When the queue is empty, it flushes deferred items back to the queue, deletes the lock, and closes itself.

## Testing

The repo ships `fake_server.py`, a small HTTP server that simulates download failures so the queue and retry logic can be exercised without a real network. It needs Python 3 and `curl` on your PATH, and picks `cfd.bat` on Windows or `cfd.sh` on Linux automatically (`--impl <path>` to point it at a specific launcher).

**Interactive mode** — start a server you configure by hand:

```
python fake_server.py                 # port 8765 (override with --port)
```

Endpoints:

- `GET /file/<name>` — serves `<name>` or fails it, depending on configuration
- `GET /configure?name=<n>&fail_count=<k>` — make `<n>` fail its next `k` requests
- `GET /configure?name=<n>&fail_count=<k>&partial_hits=<h>&partial_bytes=<b>` — the first `h` of those failures are *truncated* transfers: they write `b` bytes to disk and then drop the connection, so the partial file grows while curl still exits non-zero
- `GET /state` — JSON snapshot of fail counts and the request log
- `GET /reset` — clear state

**Automated test suite** — runs twelve scenarios end-to-end:

```
python fake_server.py --test
```

The runner starts the server on an ephemeral port, pre-seeds a `.cfd.queue` per scenario, runs the launcher (with `CFD_WORKER_MODE=inline` for the CLI scenarios so nothing pops a new window), and captures the worker log, server request log, downloaded files and final queue/lock state. To keep it fast it sets `CFD_SMALL_DELAY=0` and `CFD_WORKER_EXIT_DELAY=0` (the real defaults are 10s between retries and a 5s linger).

Scenarios:

| Scenario | Setup | What it verifies |
|---|---|---|
| `01-single-success` | 0 failures | A clean download completes and the queue empties |
| `02-fail-then-succeed` | 3 failures | Small retry budget: 1 initial attempt + 3 retries, then success |
| `03-exhaust-and-defer` | 10 failures + one clean item | An item that exhausts its full budget (1 + 3 + 3 + 3) is deferred to the lock file and restored to the queue as `[E:3]` when the worker exits; the other item still succeeds |
| `04-flag-stripped-on-startup` | 4 failures, queue seeded with an `[E:2]` prefix | Session startup strips `[E:n]` prefixes, so the item gets a fresh retry budget and eventually succeeds |
| `05-progress-resets-retries` | 12 failures, each writing 100 bytes before the connection drops | Every attempt leaves new bytes, so the retry counters reset every time and the item is never deferred — it takes 13 attempts and downloads in full. Under the old logic it would have been parked at attempt 10 and never finished |
| `06-progress-then-stall` | 13 failures, only the first 3 write bytes | Progress buys the item extra attempts (12 instead of 10), but once the attempts stop writing anything the normal deferral still kicks in |
| `07-explicit-output-name` | 12 truncated failures, command uses `curl -o renamed.bin` | The progress check follows the file the command actually writes, not the URL's basename |
| `08-two-items-mixed-progress` | One stalling item + one clean item in the same folder | Each item is measured on its own file, so a sibling's completed 1344-byte download is never mistaken for progress on the stalling one |
| `09-url-with-query-string` | 12 truncated failures on `.../item10.bin?download=true` | The query string is dropped when the target file is derived, so the download-button style URL is tracked correctly |
| `10-cli-guard-while-worker-holds-folder` | A `.cfd.lock` is already present | Typing more URLs does not start a second worker; they join the queue at positions 1 and 2 |
| `11-cli-continue-after-drained-retries` | Queue seeded with an `[E:3]` item | `cfd continue` restarts the worker over a queue whose retries ran out, and the item then succeeds |
| `12-cli-clear` | Queue seeded with one item | `cfd clear` empties the queue and the in-progress marker, and a later `cfd <url>` starts fresh at position 1 |

Per-scenario artifacts (`observations.json` plus the downloaded files) sit in `Tests/<timestamp>/<scenario>/`; the `worker.log` and `server.log` for each scenario are written next to it rather than inside it, so the worker's own output cannot be misread as download progress. That `Tests/` folder is git-ignored.

`parity_check.py` is a small helper that diffs every message the classic pure-batch `cfd.bat` (read from git `HEAD`) echoed against every `print()` in `cfd-core.py`, so the port can be checked for lost wording without running anything.

## License

Apache-2.0
