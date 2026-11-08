"""Message parity check: does cfd-core.py print everything the classic
cfd.bat echoed? Run:  py -3 parity_check.py
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def norm(s: str) -> str:
    """Make a .bat echo and a Python print comparable."""
    s = s.replace("^", "").replace("%~dp0", "")
    s = re.sub(r"%%[A-Za-z]+", "", s)          # loop variables
    s = re.sub(r"%[A-Za-z0-9_]+%", "{}", s)    # %WDIR%
    s = re.sub(r"![A-Za-z0-9_]+!", "{}", s)    # delayed expansion
    s = re.sub(r"\{[^{}]*\}", "{}", s)         # f-string placeholders
    s = re.sub(r"\{\}", "", s)                 # placeholders carry no text
    s = re.sub(r"\s+", " ", s).strip()
    return s


def bat_messages() -> list[str]:
    raw = subprocess.run(
        ["git", "show", "HEAD:cfd.bat"], capture_output=True, text=True, check=True
    ).stdout
    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        # `echo ...` on its own, or `do echo ...` inside a for loop.
        if s.startswith("echo "):
            msg = s[5:].strip()
        elif " do echo " in s:
            msg = s.split(" do echo ", 1)[1].strip()
        else:
            continue
        if "findstr" in msg:
            continue
        # "echo marker > "file"" writes to a file, it is not a message.
        redirect = re.search(r">\s*\"?%", msg)
        if redirect:
            msg = msg[:redirect.start()]
        if re.fullmatch(r"[=-]+", msg):
            continue
        out.append(norm(msg))
    # PowerShell Write-Host strings inside the .bat count as messages too.
    for chunk in re.findall(r"Write-Host '([^']*)'", raw):
        out.append(norm(chunk))
    return [m for m in out if m]


def core_messages() -> list[str]:
    src = (HERE / "cfd-core.py").read_text(encoding="utf-8")
    out: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if not s.startswith("print("):
            continue
        for chunk in re.findall(r'"([^"]*)"', s):
            chunk = chunk.replace("\\n", "").strip()
            if not chunk or re.fullmatch(r"[=-]+", chunk.replace("{0}", "")):
                continue
            out.append(norm(chunk))
        for chunk in re.findall(r"'([^']*)'", s):
            chunk = chunk.replace("\\n", "").strip()
            if chunk:
                out.append(norm(chunk))
    return out


def main() -> int:
    bat = bat_messages()
    core = core_messages()
    core_set = set(core)
    # Anything the core writes to a file (the lock marker) counts as said too.
    core_source = norm((HERE / "cfd-core.py").read_text(encoding="utf-8"))

    missing = [m for m in bat
               if m not in core_set and m not in core_source]
    extra = [c for c in core if c not in set(bat) and not any(c in b for b in bat)]

    print(f"classic .bat messages: {len(bat)}   core messages: {len(core)}")
    print("\nSaid by the .bat but not by the core:")
    for m in missing:
        print(f"  {m}")
    if not missing:
        print("  (none)")
    print("\nExtra in the core (Linux/background-mode additions):")
    for c in extra:
        print(f"  {c}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
