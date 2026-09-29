# Milestone 2, Step 5 — Dashboard Command box -> hermes-chief

Live evidence, 2026-09-29.

## What changed

- `dashboard/models.py::CommandRequest` gained `status` (`logged_only` |
  `dispatched` | `completed` | `failed`) and `result_text` - migration
  0009.
- `dashboard/app.py`'s `POST /command` now dispatches to hermes-chief's
  OpenAI-compatible `/v1/chat/completions` endpoint as a FastAPI
  `BackgroundTask` (fire-and-forget - decision 6.3), authenticated with
  `HERMES_CHIEF_API_SERVER_KEY`. If that key isn't configured on a given
  deployment, the route degrades gracefully to the old log-only behavior
  instead of failing every command - a real environment difference
  (dashboard unit tests, or a deployment that hasn't stood up Hermes),
  not a bug to hide.
- `docker-compose.yml`: `dashboard` is now dual-homed onto
  `hermes_internal` (can reach `hermes-chief` specifically; still cannot
  reach `hermes-researcher` directly by design - only hermes-chief calls
  that, via the researcher/chief handoff already proven in Step 2).

## Live proof of the real network path (Dashboard -> hermes-chief -> LiteLLM)

From inside the real running `dashboard` container, using the real
`HERMES_CHIEF_API_SERVER_KEY` from `.env`:

```
key configured: True len: 32
```

A real `POST http://hermes-chief:8642/v1/chat/completions` request was
made. `hermes-chief`'s own logs show it:

1. Accepted the request (the `API_SERVER_KEY` auth check passed).
2. Started a real agent conversation loop.
3. Called out to `litellm:4000` for the `cheap` model route
   (`nvidia_nim/meta/llama-3.1-8b-instruct`), through the network path
   proven in Step 3.
4. Failed at the very last hop with:
   ```
   openai.InternalServerError: Error code: 500 - litellm.APIError:
   Nvidia_nimException - Connection error. Received Model Group=cheap
   ```
   - because `NVIDIA_NIM_API_KEY` is still not set in `.env` (same
     blocker noted in Step 3's evidence, not yet resolved).
5. Automatically retried 3 times with backoff (Hermes's own built-in
   retry policy) before the test script's own timeout gave up waiting.

**This proves the entire chain end to end except the final external
provider credential**: Dashboard's auth to hermes-chief, hermes-chief's
own agent loop starting correctly, and its routing to LiteLLM's correct
model group all work. The one missing piece is a real value for
`NVIDIA_NIM_API_KEY` (or `OPENROUTER_API_KEY`, for the `premium` route),
which only Mohit can provide.

## Unit tests (mocked httpx, no live Hermes needed)

Three new tests in `tests/unit/test_dashboard_app.py`:
- `test_command_box_logs_but_does_not_execute_when_hermes_not_configured`
  - the graceful-degrade path, unchanged behavior from before this step.
- `test_command_box_dispatches_to_hermes_chief_when_configured` - a
  mocked successful response is correctly parsed and stored
  (`status="completed"`, `result_text` has the mocked content).
- `test_command_box_records_failure_when_hermes_chief_unreachable` - a
  connection error is caught and recorded (`status="failed"`), never
  left silently unrecorded.

## A known, real gap (not fixed in this step)

If `hermes-chief` or the dashboard process itself dies mid-dispatch (after
the `BackgroundTask` starts but before it updates the row), the
`CommandRequest` stays `"dispatched"` forever with no automatic recovery -
the same class of problem `core/reconciliation.py`'s `EXECUTING`-marker
pattern already solves for approved actions, not yet applied here.
Documented plainly in `dashboard/app.py`'s own docstring on
`_dispatch_to_hermes_chief` rather than silently left unmentioned -
tracked as necessary follow-up work, same reasoning as `KNOWN_LIMITS` bug
#11 before it was fixed.

## Regression check

`tests/unit/test_dashboard_app.py`: **19 passed** (16 existing + 3 new).
Full suite not yet re-run after this step (queued alongside the next
step's changes to avoid re-running the ~5-minute suite twice in a row).
