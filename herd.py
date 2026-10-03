#!/usr/bin/env python3
"""ZIM-CIVILIZATION — herd harness engine.

Drives N `opencode` workers across a pool of free models.

Rotation ladder, exactly as modelled in viz.html:
    model hits free-tier limit  ->  rotate to next model in the pool
    pool drained for that account  ->  fail over to next account profile, wrap to model 0

Workers talk to each other over the filesystem bus (bus.py), reached through
the `herd-send` / `herd-inbox` shims this module installs on their PATH.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import bus as busmod  # noqa: E402

STATE = ROOT / "herd.state.json"


# --------------------------------------------------------------------------- config

def load_cfg(path: Path | None = None) -> dict:
    cfg = json.loads((path or ROOT / "herd.config.json").read_text())
    cfg.setdefault("agents", 3)
    cfg.setdefault("profiles", ["bounty"])
    cfg.setdefault("cwd", str(ROOT / "workspace"))
    lim = cfg.setdefault("limits", {})
    lim.setdefault("run_timeout", 300)
    lim.setdefault("max_consecutive_errors", 3)
    lim.setdefault("cooldown", 3.0)
    cfg.setdefault("limit_patterns", ["429", "rate limit", "quota", "exceeded"])

    cfg["bin_dir"] = str(ROOT / "bin")
    pool, caps = [], {}
    for m in cfg.get("model_pool", []):
        if isinstance(m, dict):
            pool.append(m["id"])
            caps[m["id"]] = m.get("soft_cap")
        else:
            pool.append(m)
            caps[m] = None
    cfg["pool"] = pool
    cfg["caps"] = caps
    return cfg


def fmt(n) -> str:
    n = float(n or 0)
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}k"
    return str(int(n))


# --------------------------------------------------------------------------- environment

def profile_env(cfg: dict, profile: str, agent: str) -> dict:
    """Per-agent XDG dirs.

    Each worker must own its data/state/cache dir: opencode keeps a SQLite db
    there, and several processes on one file serialise into "database is
    locked" failures. Config stays shared per profile so credentials and
    settings apply to every agent using that account.
    """
    base = Path(cfg["profiles_dir"]) / profile
    shared_cfg = base / "config"
    shared_data = base / "data"
    mine = base / "agents" / agent
    for d in (shared_cfg, mine / "data", mine / "state", mine / "cache"):
        d.mkdir(parents=True, exist_ok=True)

    # inherit any account credentials the shared profile holds
    src = shared_data / "opencode" / "auth.json"
    if src.exists():
        dst = mine / "data" / "opencode"
        dst.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst / "auth.json")
        except Exception:
            pass

    env = os.environ.copy()
    env.update(
        XDG_CONFIG_HOME=str(shared_cfg),
        XDG_DATA_HOME=str(mine / "data"),
        XDG_STATE_HOME=str(mine / "state"),
        XDG_CACHE_HOME=str(mine / "cache"),
        HERD_ROOT=str(ROOT),
        HERD_AGENT=agent,
        HERD_PROFILE=profile,
    )
    opc_dir = str(Path(cfg["opencode_bin"]).parent)
    env["PATH"] = os.pathsep.join([cfg["bin_dir"], opc_dir, env.get("PATH", "")])
    return env


def profile_has_creds(cfg: dict, profile: str) -> bool:
    base = Path(cfg["profiles_dir"]) / profile
    for cand in (base / "data" / "opencode" / "auth.json",
                 base / "config" / "opencode" / "auth.json"):
        try:
            if cand.exists() and json.loads(cand.read_text()):
                return True
        except Exception:
            pass
    return False


def ensure_bin(cfg: dict) -> None:
    """Install the herd-send / herd-inbox shims agents call to reach each other."""
    b = Path(cfg["bin_dir"])
    b.mkdir(parents=True, exist_ok=True)
    scripts = {
        "herd-send": '#!/bin/sh\nexec python3 "$HERD_ROOT/bus.py" send "${HERD_AGENT:-unknown}" "$@"\n',
        "herd-inbox": '#!/bin/sh\nexec python3 "$HERD_ROOT/bus.py" drain "${HERD_AGENT:-unknown}"\n',
        "herd-peers": '#!/bin/sh\nexec python3 "$HERD_ROOT/bus.py" list\n',
    }
    for name, body in scripts.items():
        p = b / name
        p.write_text(body)
        p.chmod(0o755)


def write_state(cfg: dict, agents: list[str]) -> None:
    STATE.write_text(json.dumps({
        "agents": agents,
        "pool": cfg["pool"],
        "profiles": cfg["profiles"],
        "ts": time.time(),
    }, indent=2))


# --------------------------------------------------------------------------- one run

class RunOut:
    __slots__ = ("tokens", "cost", "text", "error", "seconds", "raw_error")

    def __init__(self, tokens=0, cost=0.0, text="", error=None, seconds=0.0, raw_error=""):
        self.tokens = tokens
        self.cost = cost
        self.text = text
        self.error = error
        self.seconds = seconds
        self.raw_error = raw_error


def describe_error(ev: dict) -> str:
    e = ev.get("error") or {}
    name = e.get("name") or "Error"
    data = e.get("data") or {}
    msg = data.get("message") or e.get("message") or ""
    ref = data.get("ref")
    out = f"{name}: {msg}".strip().rstrip(":")
    if ref:
        out += f" [{ref}]"
    return out


def is_limit(cfg: dict, blob: str) -> bool:
    low = blob.lower()
    return any(p.lower() in low for p in cfg["limit_patterns"])


def run_once(cfg: dict, profile: str, agent: str, model: str, message: str,
             on_event=None) -> RunOut:
    """Run one opencode turn. Streams parsed JSON events to on_event()."""
    env = profile_env(cfg, profile, agent)
    Path(cfg["cwd"]).mkdir(parents=True, exist_ok=True)
    cmd = [cfg["opencode_bin"], "run", "--format", "json", "-m", model, message]
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, cwd=cfg["cwd"], env=env, text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
        )
    except FileNotFoundError:
        return RunOut(error=f"opencode not found at {cfg['opencode_bin']}")

    acc = {"tokens": 0, "cost": 0.0, "texts": [], "err": None, "blob": ""}

    def read_stdout():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                acc["blob"] += line + "\n"
                continue
            t = ev.get("type")
            part = ev.get("part") or {}
            if t == "text":
                txt = part.get("text")
                if txt:
                    acc["texts"].append(txt)
            elif t == "step_finish":
                tk = part.get("tokens") or {}
                acc["tokens"] += tk.get("total") or 0
                acc["cost"] += part.get("cost") or 0.0
            elif t == "error":
                acc["err"] = describe_error(ev)
                acc["blob"] += json.dumps(ev)
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass

    def read_stderr():
        try:
            acc["blob"] += proc.stderr.read() or ""
        except Exception:
            pass

    ts = threading.Thread(target=read_stdout, daemon=True)
    te = threading.Thread(target=read_stderr, daemon=True)
    ts.start()
    te.start()

    timeout = cfg["limits"]["run_timeout"]
    ts.join(timeout)
    timed_out = ts.is_alive()
    if timed_out:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    te.join(3)

    err = acc["err"]
    blob = acc["blob"]
    if timed_out:
        err = err or f"timeout after {timeout}s"
        blob += " timeout"
    if proc.returncode not in (0, None) and not err:
        tail = (blob or "").strip().splitlines()
        err = f"exit {proc.returncode}: {tail[-1][:200] if tail else 'no output'}"

    return RunOut(
        tokens=acc["tokens"],
        cost=acc["cost"],
        text="".join(acc["texts"]).strip(),
        error=err,
        seconds=time.time() - t0,
        raw_error=blob[:600],
    )


# --------------------------------------------------------------------------- worker

class Worker(threading.Thread):
    """One opencode instance with its own account + model cursor."""

    def __init__(self, engine: "Engine", name: str, acct_idx: int = 0, model_idx: int = 0):
        super().__init__(daemon=True)
        self.engine = engine
        self.name = name
        self.acct_idx = acct_idx
        self.model_idx = model_idx
        self.profile = engine.cfg["profiles"][acct_idx % len(engine.cfg["profiles"])]
        self.status = "idle"          # idle | running | limit | switching
        self.task = "—"
        self.last = ""
        self.tok_session = 0
        self.tok_model = 0
        self.switches = 0
        self.limits = 0
        self.errors = 0
        self.queue: list[str] = []
        self._qlock = threading.Lock()
        self.stop_flag = False
        self.lines: list[tuple[str, str, str]] = []   # per-agent stream for the muxer

    def alog(self, kind: str, msg: str):
        """Log to this agent's own stream only — its pane's transcript."""
        ts = time.strftime("%H:%M:%S")
        with self.engine.lock:
            self.lines.append((kind, ts, msg))
            if len(self.lines) > 400:
                del self.lines[:100]

    def nlog(self, kind: str, msg: str):
        """Log to the engine's event stream and this agent's own stream."""
        self.alog(kind, msg)
        self.engine.log(kind, msg)

    # -- queue
    def enqueue(self, msg: str):
        with self._qlock:
            self.queue.append(msg)

    def _take_queue(self) -> list[str]:
        with self._qlock:
            q, self.queue = self.queue, []
            return q

    # -- rotation
    @property
    def model(self) -> str:
        return self.engine.cfg["pool"][self.model_idx]

    def quota(self) -> float:
        cap = self.engine.cfg["caps"].get(self.model) or 0
        return min(self.tok_model / cap, 1.0) if cap else 0.0

    def rotate(self, reason: str):
        prev = self.model
        self.model_idx += 1
        failed_over = False
        if self.model_idx >= len(self.engine.cfg["pool"]):
            self.model_idx = 0
            self.acct_idx += 1
            self.profile = self.engine.cfg["profiles"][self.acct_idx % len(self.engine.cfg["profiles"])]
            self.engine.acct_rot += 1
            failed_over = True
        self.tok_model = 0
        self.switches += 1
        self.engine.switches += 1
        self.status = "switching"
        if failed_over:
            self.nlog("acct", f"pool drained -> account failover to '{self.profile}'")
        self.nlog("rot", f"{prev} -> {self.model}  ({reason})")

    # -- one turn with the rotation ladder
    def run_message(self, message: str):
        if not self.engine.cfg["pool"]:
            return
        attempts = 0
        ceiling = len(self.engine.cfg["pool"]) * max(len(self.engine.cfg["profiles"]), 1) + 3
        while attempts < ceiling and not self.stop_flag and not self.engine.stop:
            attempts += 1
            model = self.model
            self.status = "running"
            self.task = message if len(message) <= 90 else message[:89] + "…"
            # Show what was asked in the agent's own stream, so its pane reads
            # as a prompt/reply transcript rather than a wall of status lines.
            self.alog("prompt", f"› {message}")
            self.nlog("run", f"-> {model}  [{self.profile}]")

            out = run_once(self.engine.cfg, self.profile, self.name, model,
                           message, on_event=self.engine.on_event)

            self.tok_session += out.tokens
            self.tok_model += out.tokens
            self.engine.tokens += out.tokens
            self.engine.cost += out.cost

            blob = f"{out.error or ''} {out.raw_error}"

            if out.error and is_limit(self.engine.cfg, blob):
                self.status = "limit"
                self.limits += 1
                self.engine.limits += 1
                self.nlog("limit", f"429/limit on {model} after {fmt(out.tokens)} tok")
                time.sleep(0.6)
                self.rotate("free-tier limit")
                time.sleep(self.engine.cfg["limits"]["cooldown"])
                continue

            if out.error:
                self.errors += 1
                self.engine.errors += 1
                self.status = "idle"
                self.nlog("err", f"{out.error}")
                if self.errors >= self.engine.cfg["limits"]["max_consecutive_errors"]:
                    self.errors = 0
                    self.rotate("repeated errors")
                    time.sleep(self.engine.cfg["limits"]["cooldown"])
                continue

            # success
            self.errors = 0
            self.status = "idle"
            reply = (out.text or "").strip()
            self.last = reply.replace("\n", " ")[:300]
            self.nlog("ok", f"done {fmt(out.tokens)} tok on {model}")
            # The full answer goes into the agent's own stream, one entry per
            # line. The shared event log gets only a one-line pointer, or it
            # would drown in model output once replies are no longer truncated.
            if reply:
                for ln in reply.splitlines():
                    self.alog("reply", ln)
                self.engine.log("reply", f"{self.name} ↳ {reply.splitlines()[0][:120]}")
            self.engine.record(self.name, model, message, reply, out.tokens)
            # a successful turn may itself have triggered a limit mid-stream elsewhere;
            # honour the soft cap as an early hint only when no error surfaced
            if self.quota() >= 1.0:
                self.nlog("warn", f"soft cap reached on {model} (rotating early)")
                self.rotate("soft cap")
            self.status = "idle"
            return

        self.status = "idle"
        self.nlog("err", f"gave up after {attempts} attempts")

    def run(self):
        while not self.stop_flag and not self.engine.stop:
            msgs = busmod.drain(self.name)
            queued = self._take_queue()
            if msgs or queued:
                parts = []
                for m in msgs:
                    parts.append(f"{m.get('from', '?')} says: {m.get('msg', '')}")
                parts.extend(queued)
                self.run_message("\n".join(parts))
            else:
                # Nothing queued: any leftover limit/switching state is stale.
                # rotate() sets "switching" for visibility, so it must be
                # cleared here or the worker wedges and --until-idle never fires.
                self.status = "idle"
                time.sleep(0.4)


# --------------------------------------------------------------------------- engine

class Engine:
    def __init__(self, cfg: dict, n_agents: int | None = None, headless: bool = True):
        self.cfg = cfg
        self.headless = headless
        self.stop = False
        self.lock = threading.RLock()   # reentrant: log() is called from inside locked blocks
        self.tokens = 0
        self.cost = 0.0
        self.switches = 0
        self.limits = 0
        self.acct_rot = 0
        self.errors = 0
        self.t0 = time.time()
        self.log_lines: list[tuple[str, str, str]] = []   # (kind, ts, msg)
        self.results: list[dict] = []

        n = n_agents or cfg["agents"]
        self.workers = [
            Worker(self, f"agent-{i+1}", acct_idx=i % max(len(cfg["profiles"]), 1),
                   model_idx=i % max(len(cfg["pool"]), 1))
            for i in range(n)
        ]
        write_state(cfg, [w.name for w in self.workers])

    # -- plumbing
    def log(self, kind: str, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.log_lines.append((kind, ts, msg))
            if len(self.log_lines) > 600:
                del self.log_lines[:200]

    def record(self, agent, model, prompt, reply, tokens):
        with self.lock:
            self.results.append({
                "ts": time.time(), "agent": agent, "model": model,
                "prompt": prompt[:400], "reply": (reply or "")[:20000], "tokens": tokens,
            })
            try:
                with open(ROOT / "herd.results.jsonl", "a") as f:
                    f.write(json.dumps(self.results[-1]) + "\n")
            except Exception:
                pass

    def on_event(self, ev: dict):
        """Live per-step hook; kept cheap since it fires for every event."""
        pass

    def broadcast(self, msg: str):
        for w in self.workers:
            w.enqueue(msg)
        self.log("prompt", f"broadcast -> {', '.join(w.name for w in self.workers)}: {msg[:100]}")

    def start(self):
        for w in self.workers:
            w.start()

    def uptime(self) -> str:
        s = int(time.time() - self.t0)
        return f"{s//60:02d}:{s%60:02d}"


# --------------------------------------------------------------------------- cli

def cmd_doctor(cfg: dict) -> int:
    ok = True
    print("ZIM-CIVILIZATION // doctor\n" + "-" * 46)

    binp = Path(cfg["opencode_bin"])
    if binp.exists():
        print(f"  opencode        OK   {binp}")
    else:
        print(f"  opencode        FAIL not found at {binp}")
        ok = False

    print(f"  profiles dir    {cfg['profiles_dir']}")
    for p in cfg["profiles"]:
        has = profile_has_creds(cfg, p)
        d = Path(cfg["profiles_dir"]) / p
        mark = "OK  " if has else "WARN"
        print(f"    - {p:<12} {mark} dir={'yes' if d.exists() else 'no ':<3} credentials={'yes' if has else 'NO'}")
        if not has:
            print(f"      -> provision with:  XDG_CONFIG_HOME={d}/config "
                  f"XDG_DATA_HOME={d}/data {cfg['opencode_bin']} auth login")
    print(f"  model pool      {len(cfg['pool'])} free models")
    for m in cfg["pool"]:
        print(f"    - {m}")
    print(f"  agents          {cfg['agents']}")
    print(f"  workspace       {cfg['cwd']}")

    ensure_bin(cfg)
    print(f"  shims           {cfg['bin_dir']}/herd-send, herd-inbox, herd-peers")

    authed = [p for p in cfg["profiles"] if profile_has_creds(cfg, p)]
    if not authed:
        print("\n  NOTE: no profile has credentials. The free model pool runs")
        print("        anonymously (verified: cost=0 without auth), so agents will")
        print("        still work — but every 'account' is then the same identity,")
        print("        so account failover is cosmetic until you add real logins.")
    else:
        print(f"\n  credentials present in: {', '.join(authed)}")

    print("  VERDICT: ready." if ok else "  VERDICT: issues above.")
    return 0 if ok else 1


def cmd_run(cfg: dict, args) -> int:
    ensure_bin(cfg)
    eng = Engine(cfg, n_agents=args.agents, headless=not args.tui)

    if args.prompt:
        for line in args.prompt.split(";;"):
            if line.strip():
                eng.broadcast(line.strip())

    eng.start()

    if args.tui:
        try:
            import herdmux
            return herdmux.TUI(eng).loop()
        except Exception as e:
            print(f"[tui unavailable: {e}] falling back to headless", file=sys.stderr)

    seen = 0
    kinds = {"limit": "\033[31m", "rot": "\033[33m", "acct": "\033[36m",
             "ok": "\033[32m", "err": "\033[31m", "prompt": "\033[35m",
             "run": "\033[90m", "warn": "\033[33m"}
    try:
        while True:
            time.sleep(0.5)
            with eng.lock:
                pending = eng.log_lines[seen:]
                seen = len(eng.log_lines)
            for kind, ts, msg in pending:
                print(f"{kinds.get(kind, '')}{ts} {msg}\033[0m", flush=True)
            if getattr(args, "until_idle", False) and all(
                    w.status == "idle" and not w.queue for w in eng.workers) and seen:
                time.sleep(1.5)
                break
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop = True
        for w in eng.workers:
            w.stop_flag = True
        time.sleep(0.6)
    print(f"\ntokens={fmt(eng.tokens)} cost=${eng.cost:.4f} "
          f"switches={eng.switches} limits={eng.limits} acct_rot={eng.acct_rot}")
    return 0


def cmd_broadcast(cfg: dict, args) -> int:
    targets = busmod.send("operator", "all", args.message)
    print(f"delivered to: {', '.join(targets)}")
    return 0


def cmd_ps(cfg: dict) -> int:
    if not STATE.exists():
        print("no herd.state.json — engine not started yet")
        return 1
    print(STATE.read_text())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="herd", description="ZIM-CIVILIZATION herd harness")
    ap.add_argument("-c", "--config", default=None)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", help="check environment, accounts, models")
    p = sub.add_parser("run", help="start the herd")
    p.add_argument("--prompt", default=None, help="prompt to broadcast at boot")
    p.add_argument("--agents", type=int, default=None)
    p.add_argument("--tui", action="store_true", help="launch the multiplexer UI")
    p.add_argument("--until-idle", action="store_true", help="exit once every worker is idle")
    p = sub.add_parser("send", help="send a message to the herd bus")
    p.add_argument("message")
    sub.add_parser("ps", help="show herd state file")

    args = ap.parse_args()
    cfg = load_cfg(Path(args.config) if args.config else None)

    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "run":
        return cmd_run(cfg, args)
    if args.cmd == "send":
        return cmd_broadcast(cfg, args)
    if args.cmd == "ps":
        return cmd_ps(cfg)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
