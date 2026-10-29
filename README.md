# Agent Ultra 2

Agent Ultra 2 is a Python agent built on the grok-bot repo (ruflo)
base. A head hires workers, runs copies of a task through an arena
of distinct approaches in waves, judges exactly one winner, and
repeats work in a convergence loop until all results agree or a
full update is signalled. Every copy can drive its own live
browser: one Playwright/Chromium session per copy, each with its
own persistent profile, plus a live screen panel.

This repo is a local build only. Nothing here is live or deployed
until it is deployed and its `/health` endpoint is verified.

## Layout

```
agent-ultra-2/
  README.md                  this file
  PLAN_MAP_STRUCTURE.md      plan, map, structure (wave 0)
  ARCHITECTURE.md            structure, tunnel, pipeline, architecture
  agent/                     head, workers, convergence loop, judge
  browser/                   live browser: session, actions, CDP,
                             screencast, per-copy profiles
  copies/                    arena waves + 16 distinct approaches
  tools/                     tool pipeline + JSON-backed registry
  memory/                    local JSONL memory store
  server/                    HTTP server, chat UI, browser panel, API
    main.py                  entrypoint: python3 -m server.main
    chat_server.py           routes + per-copy browser sessions
    model_client.py          backend client (environment only)
    model_loop.py            messages + one browser tool, max 3 rounds
    supervisor.py            crash restart + checkpoint
    health.py                real health checks
    ui/                      chat page + live browser panel
  deploy/                    Render Free config (render.yaml, Dockerfile)
  tests/                     stdlib-only tests, each runnable directly
  ruflo-base/                base repo reference (read-only input)
```

## Run locally

Requirements: Python 3.12, stdlib only for the server itself.
Playwright + Chromium are needed only for a real browser; without
them the browser endpoints fail honestly instead of faking results.

```sh
cd agent-ultra-2
python3 -m server.main
```

The server listens on `$PORT` (8000 if unset) and serves:

| Endpoint                  | What it does                                              |
| ------------------------- | --------------------------------------------------------- |
| `GET /health`             | Real checks: supervisor, memory, tools, disk              |
| `GET /ui`                 | Chat page + live browser panel                            |
| `GET /api/browser/screenshot` | Live PNG from the active session, or honest 404      |
| `POST /api/browser/action`| `{"action", "args", "copy_id"}` on that copy's browser    |
| `GET /api/tools`          | Registered tools: name + argument names only              |
| `POST /api/chat`          | `{"messages"}` through the model loop                     |
| `POST /api/arena/run`     | `{"task_id", "copies", "max_concurrent"}` (defaults 4/4)  |

## Backend configuration

The model backend slot is empty on purpose. It is configured only
through the environment, and no values live in this repo:

- `MODEL_API_URL` — full chat-completions endpoint URL
- `MODEL_ID` — model identifier sent in the request body

With either unset, `/api/chat` answers honestly:

```json
{"reply": "model not live yet", "live": false}
```

No reply is ever fabricated. The model loop passes the caller's
messages unchanged, offers exactly one tool (`browser_open`,
carrying name plus parameter schema only), runs a real browser
goto for a tool call (measured milliseconds fed back as a tool
message, max 3 tool rounds), and never executes an unknown tool
name. No model-facing text is written anywhere in this repo.

## Deploy on Render (Free)

`deploy/render.yaml` + `deploy/Dockerfile` describe a Free web
service whose entrypoint is `python3 -m server.main` on `$PORT`.
To deploy: create the service from this repo in the Render
dashboard, leave the plan on Free, and set `MODEL_API_URL` and
`MODEL_ID` in the dashboard (both are `sync: false` in
`render.yaml`, so no values are stored in the repo). Then verify
`/health` on the deployed URL before calling anything live.

## Tests

Each test file runs directly with plain Python, stdlib only, from
the repo root:

```sh
python3 tests/test_server.py
python3 tests/test_integration.py
```

Real-browser legs skip honestly (not fail) when the Playwright
package or a Chromium binary is absent in system Python.

## Honest limits

- Render Free sleeps on inactivity and has 512 MB RAM, so it is
  not 24/7 and not a fleet host. Always-on or fleet hosting needs
  a larger host, which is a paid step and is not done here.
- Cloudflare and captcha blocks are attempted like a normal
  browser and never promised away: a blocked page surfaces as its
  real error or page state, never as a bypass.
- 100 copies run in waves (sequential batches of at most
  `max_concurrent` on one host), not simultaneously on one host.
- The live screen is a screenshot/screencast panel in the UI. It
  is not Muse's in-chat browser card, which is a product feature
  that cannot be copied.
- The tool pipeline validates and tests pure-computation tools;
  it is not a sandbox. A registered tool runs in-process with the
  caller's own privileges.
- No secrets, tokens, passwords, cookies, or profile data live in
  this repo. Browser profiles live outside the repo under the
  cache directory, and sign-in is done by the owner through the
  browser, never automated here.
- No accelerator is required or used, and nothing here is paid.
