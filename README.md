# ZIM-CIVILIZATION

A herd harness: N `opencode` agents running in parallel across a pool of free
models, rotating model → then account as free-tier limits bite, and talking to
each other over a filesystem message bus.

## What's here

| file | what it is |
|---|---|
| `viz.html` | the design mock — open in a browser. Black CRT dashboard, 5 agent tiles, animated rotation. |
| `herd.py` | the engine: workers, rotation ladder, token accounting, CLI |
| `herdmux.py` | the multiplexer UI (Ctrl-B prefix) — replaces tmux |
| `bus.py` | inter-agent mailbox (append-only jsonl + flock) |
| `bin/` | `herd-send`, `herd-inbox`, `herd-peers` shims, auto-installed |
| `herd.config.json` | models, profiles, caps, limits |

## Run it

```sh
cd ~/zim-civilization
python3 herd.py doctor                          # verify environment
python3 herd.py run --agents 3 --tui            # interactive multiplexer
python3 herd.py run --agents 3 \
    --prompt "refactor the auth module ;; write tests"   # headless
python3 herd.py send "status report"            # poke a running herd
```

`--until-idle` exits once every worker drains its queue (useful for scripting).

## Keybindings (prefix = Ctrl-B)

| key | action |
|---|---|
| `c` | new window |
| `n` / `p` | next / previous window |
| `0-9` | select window by number |
| `h` | split horizontal (top/bottom) |
| `v` | split vertical (side by side) |
| `o` / arrows | cycle / move pane focus |
| `t` | change pane content (dashboard → agent → log → pool) |
| `x` | kill pane |
| `z` | zoom pane |
| `r` | force model rotation on the focused agent |
| `?` | help |
| `d` | detach / quit |
| `Enter` | focus the prompt line — `Tab` toggles broadcast vs. focused agent |

## The rotation ladder

```
model hits its free-tier limit
      └─→ rotate to the next model in the pool
              └─→ pool drained → fail over to the next account profile
                      └─→ wrap to model 0, keep going
```

Soft caps (`soft_cap` per model in the config) rotate *before* a hard 429, so
the herd keeps moving instead of stalling. `limit_patterns` catches real 429s
in `opencode`'s stderr/JSON and triggers the same path.

## Agents talking to each other

Each worker gets `herd-send` / `herd-inbox` on its PATH plus `HERD_AGENT` set to
its own name, so an agent can do this from inside a turn:

```sh
herd-send all "found the bug in auth.ts:88, who's taking it?"
herd-inbox                     # drain my mailbox
```

Broadcasts are not echoed back to the sender (otherwise a worker would pick up
its own message and start a second turn, and broadcasts could ping-pong).

## Environment notes (this box)

Verified against the real machine, and three things differ from the original
plan — worth knowing before you wonder why something is missing:

- **No tmux.** Not installed, no sudo, no package manager access for it. Hence
  `herdmux.py`. The bindings above are the tmux ones you asked for.
- **One account, not three.** `oc-bounty` / `oc-clipforge` / `oc-zecurity` live
  in `/root/.bashrc`, which is a *different home* from `$HOME=/home/zim`, so
  your shell never sources them. Only the `bounty` profile directory exists;
  `clipforge` and `zecurity` were never created. Add more profiles and list
  them in `herd.config.json` → `profiles` when you want real failover.
- **The free pool needs no credentials.** Verified: a clean XDG dir with no
  `auth.json` still returns `cost:0`. So agents run fine today, but every
  "account" is the same anonymous identity — account failover is cosmetic until
  you add real logins. To provision one:

  ```sh
  XDG_CONFIG_HOME=$HOME/.opencode-profiles/<name>/config \
  XDG_DATA_HOME=$HOME/.opencode-profiles/<name>/data \
  /root/.opencode/bin/opencode auth login
  ```

`opencode` itself is at `/root/.opencode/bin/opencode` (v1.18.32) — not on
`PATH`; the engine passes the absolute path and puts the shims in front.

## Verified working

- 3 agents on 3 different models concurrently, `cost=$0.0000`
- rotation on soft cap: `mimo-v2.6-flash → nemotron-3.5-lightning`
- account failover on pool drain: `bounty → bounty-2`, wrapped to model 0
- tokens tracked per model / per session / globally
- agents invoking `herd-send` from *inside* a live turn; bus traffic confirmed
- multiplexer: all bindings, splits, zoom, content cycling, box rendering
- per-agent XDG isolation (without it, concurrent workers deadlock on a shared
  SQLite db: `exit 1: database is locked`)

## Adding accounts / models

```jsonc
// herd.config.json
"profiles": ["bounty", "clipforge"],      // account rotation order
"model_pool": [
  { "id": "opencode/mimo-v2.6-flash-free", "soft_cap": 150000 },
  ...
]
```

`soft_cap` is a local early-rotation threshold in tokens, not the provider's
real limit — set it under whatever the true free-tier ceiling is. Run
`opencode models` for the current list of free models.
