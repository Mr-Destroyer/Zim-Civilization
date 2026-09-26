#!/usr/bin/env python3
"""ZIM-CIVILIZATION — inter-agent message bus.

A file-backed mailbox per agent. Append-only jsonl inboxes, locked with
flock so concurrent workers can't interleave a write. Agents reach this
through the `herd-send` / `herd-inbox` shims on their PATH.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HERD_ROOT") or Path(__file__).resolve().parent)
BUS = ROOT / "bus"
BUS.mkdir(parents=True, exist_ok=True)


def _known_agents() -> list[str]:
    """Agent roster: the engine's state file if present, else existing inboxes,
    else the configured agent count (so a broadcast before boot still fans out)."""
    state = ROOT / "herd.state.json"
    if state.exists():
        try:
            names = json.loads(state.read_text()).get("agents") or []
            if names:
                return list(names)
        except Exception:
            pass
    found = sorted(p.stem for p in BUS.glob("*.jsonl"))
    if found:
        return found
    n = 3
    cfgp = ROOT / "herd.config.json"
    if cfgp.exists():
        try:
            n = int(json.loads(cfgp.read_text()).get("agents") or 3)
        except Exception:
            pass
    return [f"agent-{i+1}" for i in range(n)]


def _append(who: str, record: dict) -> None:
    with open(BUS / f"{who}.jsonl", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def send(frm: str, to: str, msg: str) -> list[str]:
    targets = _known_agents() if to in ("all", "*", "broadcast") else [to]
    if not targets:
        targets = [to]
    rec = {"ts": time.time(), "from": frm, "to": to, "msg": msg}
    for t in targets:
        if t == frm:
            # Don't echo a broadcast back to its sender: the worker would drain
            # it, start a turn, and could bounce messages indefinitely.
            continue
        _append(t, rec)
    return [t for t in targets if t != frm]


def drain(who: str) -> list[dict]:
    """Read and atomically clear an agent's inbox."""
    p = BUS / f"{who}.jsonl"
    if not p.exists():
        return []
    with open(p, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = f.read()
            f.seek(0)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    out = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def render(records: list[dict]) -> str:
    if not records:
        return ""
    lines = []
    for r in records:
        ts = time.strftime("%H:%M:%S", time.localtime(r.get("ts", time.time())))
        lines.append(f"[{ts}] from {r.get('from', '?')}: {r.get('msg', '')}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "send":
        if len(argv) < 5:
            print("usage: bus.py send <from> <to> <message...>", file=sys.stderr)
            return 2
        targets = send(argv[2], argv[3], " ".join(argv[4:]))
        print(f"delivered to: {', '.join(targets)}")
    elif cmd == "drain":
        if len(argv) < 3:
            print("usage: bus.py drain <agent>", file=sys.stderr)
            return 2
        print(render(drain(argv[2])))
    elif cmd == "list":
        print("\n".join(_known_agents()))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
