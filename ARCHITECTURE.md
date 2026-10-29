# Agent Ultra 2 — Architecture

Neat, clean, crystal clear, raw. This document describes the
structure, the tunnel, the pipeline, and the architecture, per the
plan's map (`PLAN_MAP_STRUCTURE.md`). Local build only: nothing
here is claimed live or deployed.

## 1. Components

### Head (`agent/head.py`)

The head hires workers (`hire_workers(n)`), collects reports
(`collect_reports()`), compares every report on mechanical
evidence (`compare()`), and judges (`judge()`). Judging returns
exactly one winner or `None`, plus a `missing` list for every
expected worker/copy that never reported. Nothing is dropped
silently.

### Workers (`agent/worker.py`)

One worker runs one approach for one task and returns a report
dict: `task_id, worker_id, approach, result, evidence, status,
browser_available`. The browser is injected duck-typed and
optional; a browser approach with no browser reports the browser
as unavailable and returns no result, never a faked one.

### Copies and the arena (`copies/arena.py`, `copies/approaches.py`)

`run_arena(task_id, base_dir, copies=100, max_concurrent=10)`
writes `orders.jsonl` first (one order per copy, each with its
assigned approach), then runs copies in sequential waves of at
most `max_concurrent`, appending each report to `reports.jsonl`,
then judges exactly one winner plus the missing list.

- 16 real approaches, all stdlib and runnable (calculator, text
  counts, hashing, sorting, file round-trip, shell echo, and two
  browser approaches that need a real browser object).
- Distinct approach per wave; reuse across waves only. A wave
  larger than the distinct pool, or a custom list with duplicates,
  is refused with `ValueError`.
- 100 copies run in waves, not simultaneously on one host. True
  simultaneity would need a fleet.

### Browser (`browser/`)

One live browser per copy/worker:

- `BrowserSession(profile_dir, headless=True, viewport=None)`:
  persistent Chromium context, `start()`, `page()`, `close()`,
  `act(action, **kw)` returning a real result with measured
  milliseconds and the real error on failure. Playwright is
  imported lazily inside `start()`; without it, `start()` raises
  `BrowserNotAvailable` and `act()` reports the failure, never a
  fake page state.
- `dispatch_browser_action(session, tool_name, args)` and
  `list_browser_tools()`: 28 canonical actions (goto, click,
  scroll, copy, paste, screenshot, evaluate, wait, and the rest).
  No artificial delays; selector waits are bounded (default
  2000 ms).
- `CDPClient`: direct CDP over `page.context.new_cdp_session`,
  `send(method, params)` with measured timings, plus Page /
  Network / Runtime helpers. No cookie, token, or credential
  values are read or returned.
- `Screencast`: real JPEG frames over CDP, each frame
  acknowledged (`Page.screencastFrameAck`), `stop()` ends the
  stream cleanly. No frames are fabricated.
- Profiles (`browser/profile.py`): one directory per copy under
  `~/.cache/agent-ultra-2/profiles/<copy-id>` (outside the repo,
  never committed). Sign-in is never automated and no password is
  stored; the owner signs in through the browser and Chromium
  keeps the session. Only a cookie row count is ever reported,
  never cookie values.

### Tunnel

The public route to the agent is the deploy target's URL
(Render Free, see `deploy/render.yaml`) reaching the server
process on `$PORT`. The tunnel carries HTTP only: chat API,
browser panel, and health checks. There is no separate tunnel
daemon in this repo, and no tunnel credential lives here.

### Pipeline

Two pipelines, kept separate:

1. **Work pipeline** — order, worker, browser action, evidence,
   report, head, judge, loop. Every step keeps its real evidence;
   failures are reported as failures.
2. **Tool pipeline** (`tools/pipeline.py`, 5 stages: intake,
   validated_call, execute, evidence, report) — a tool is pure
   computation only: one function, no imports, no eval/exec/open,
   no dunder access, no globals, plus passing test cases.
   Failing or colliding tools are NOT registered, and dispatch
   of an unknown tool raises a real error. Honest limit: this
   pipeline validates and tests; it is NOT a sandbox. A
   registered tool runs in-process with the caller's own
   privileges.

### Memory (`memory/store.py`)

Local JSONL store: `save(key, value, tags)` appends one record,
`recall(key or tags)` returns matching records, oldest first. No
network, no keys, no secrets; it stores only what the caller
passes and sends nothing anywhere.

### Supervisor and health (`server/supervisor.py`, `server/health.py`)

- `Supervisor`: runs a command, restarts it on crash (non-zero
  exit or start failure) with geometric backoff capped at 60 s,
  up to `max_restarts`, and writes a checkpoint JSON after every
  attempt. `status()` reports only what was observed.
- `health()`: returns `{ok, checks: {supervisor, memory, tools,
  disk}}` from real checks — supervisor file/status readable,
  memory JSONL parses, registry JSON parses, disk free > 0.
  `ok` is true only if every check passes.

### Convergence loop (`agent/loop.py`)

`ConvergenceLoop.run(task, participants, max_rounds=10)` repeats
rounds until consensus (every participant ok with equal non-None
results) or a full update (explicit signal only). A checkpoint
JSON is written after EVERY round, and a `running` checkpoint
resumes instead of restarting. Exhausting `max_rounds` stops
honestly as `not_converged` with the real partial evidence.

## 2. Server wiring (`server/`)

Stdlib HTTP only, no new dependencies. Entrypoint:

```sh
python3 -m server.main   # serves on $PORT
```

`server/main.py` runs from the repo root and ensures the repo
root is on `sys.path`, because `agent/` and `copies/` are
sibling top-level packages. `server/chat_server.py` holds the
routes; `server/model_client.py` and `server/model_loop.py`
hold the backend wiring.

Request flow for chat:

```
POST /api/chat {messages}
  -> model_loop.run(messages)
    -> model_client.chat(messages unchanged, tools=[browser_open])
      -> backend (env URL only) or honest "model not live yet"
    -> browser_open tool call?
      -> real BrowserSession goto, measured ms, fed back as a
         tool message (max 3 tool rounds)
      -> unknown tool name? not executed, reported as such
  -> {"reply", "live", "tool_rounds", "browser_results", "error"}
```

Request flow for the browser panel:

```
POST /api/browser/action {action, args, copy_id}
  -> lazy per-copy BrowserSession with that copy's own profile
  -> dispatch_browser_action -> timed real result
GET /api/browser/screenshot
  -> PNG bytes from the active session's open page
  -> 404 "no live browser session" when there is no open page
UI polls /api/browser/screenshot every 2 seconds; with no
session it shows honest text, never a placeholder image.
```

Request flow for the arena:

```
POST /api/arena/run {task_id, copies=4, max_concurrent=4}
  -> copies.run_arena into a fresh temp dir
  -> {winner, missing, comparison, reports, orders_file, reports_file}
Full 100 copies are allowed through the same endpoint via waves.
```

## 3. Data flow and storage

- Arena orders/reports: JSONL under a fresh temp dir per API run
  (returned in the response as `orders_file` / `reports_file`).
- Convergence checkpoints: JSON per loop, after every round.
- Supervisor checkpoints: JSON after every supervised attempt.
- Memory: local JSONL. Tool registry: local JSON.
- Browser profiles: `~/.cache/agent-ultra-2/profiles/<copy-id>/`.
- Nothing in this repo holds secrets, tokens, passwords, cookies,
  or profile data. The backend is configured only via
  `MODEL_API_URL` / `MODEL_ID` in the environment, with no values
  in any file.

## 4. Honest limits

- Render Free sleeps on inactivity and has 512 MB RAM, so it is
  not 24/7 and not a fleet host. Always-on or fleet hosting needs
  a larger host, which is a paid step and is not done here.
- Cloudflare and captcha blocks are attempted like a normal
  browser and never promised away. A block surfaces as the real
  error or the real page state; no bypass is attempted.
- 100 copies run in waves, not simultaneously on one host.
- The live screen is a screenshot/screencast panel in the UI. It
  is not Muse's in-chat browser card, which is a product feature
  that cannot be copied.
- The model backend slot is empty until the owner sets the two
  environment variables. Until then every chat reply is honestly
  "model not live yet".
- The tool pipeline is validation plus testing, not isolation.
- No accelerator is required or used, and nothing here is paid.
- This build is local only. Deploy claims wait for a real deploy
  plus a verified `/health` on the deployed URL.
