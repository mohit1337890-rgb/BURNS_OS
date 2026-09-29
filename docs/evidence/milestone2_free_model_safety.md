# Milestone 2 — free-model safety (Mohit's approval condition 4)

Live evidence, 2026-09-29.

## Real bugs found getting the free models actually working

- `nvidia_nim/meta/llama-3.1-8b-instruct` (the originally chosen `cheap`
  route) reached end-of-life 2026-08-26 - HTTP 410, found on the first
  real completion attempt.
- Its catalog replacement, `nvidia/llama-3.1-nemotron-51b-instruct`
  (confirmed to exist via NVIDIA's own live `/v1/models` endpoint), still
  failed - HTTP 404 `"Function not found for account"`: NVIDIA NIM's
  public catalog listing a model does not mean every individual API key
  can call it. Not documented anywhere obvious - found only by trying it
  live.
- Fixed by moving `cheap` to a second OpenRouter free model
  (`qwen/qwen3.8-27b:free`) instead of continuing to guess at NVIDIA's
  per-account access. Both `premium` and `cheap` now confirmed working
  with a real completion call.
- A genuine `429` (`qwen3.8-27b:free is temporarily rate-limited
  upstream`) happened naturally while testing - real evidence that the
  free tier does rate-limit under load, motivating the retry config below
  rather than a hypothetical concern.

## `data_class: public_only` + a real pre-call guardrail

`configs/litellm_config.yaml`'s `premium`/`cheap` entries are tagged
`model_info.data_class: public_only`. `configs/litellm_guardrails.py`
(mounted into the `litellm` container, registered via
`litellm_settings.callbacks`) is a real `CustomLogger.async_pre_call_hook`
that scans outgoing message content for sensitive patterns
(`client`, `trading strategy`, `confidential`, `proprietary`, `private`,
`burns worldwide`) and refuses the call **before it ever reaches
OpenRouter** if the target model is one of those two names - a code-level
block, not a system-prompt instruction (this project's standing design
rule).

Live test:

```
=== sensitive content (should be BLOCKED) ===
BLOCKED status=500
{"error":{"message":"Refused by PublicOnlyRouteGuardrail: model 'premium'
is a public-data-only free route (OpenRouter free tier - retention/
training policy unknown) and this request matched a sensitive-data
pattern ('client'). Use a non-free route for private/client/trading-
strategy content. ..."}}

=== clean content (should be ALLOWED) ===
ALLOWED
The capital of France is **Paris**.
```

(LiteLLM surfaces a hook's raised `ValueError` as HTTP 500, not 400 - a
LiteLLM implementation detail, not a design choice here; still a clean,
loud refusal either way, never a silent pass-through.)

This is a coarse keyword scan, not a full DLP pass (`core/dlp.py` already
owns actual secret-detection for outgoing plugin content specifically) -
it exists to catch obviously business-sensitive language before it
leaves this proxy for a free, retention-policy-unknown endpoint. A
determined attempt to phrase sensitive content around these exact
keywords could still get through - documented as a known limitation, not
a guaranteed DLP-grade filter.

## 429/rate-limit handling (already real, now also configured explicitly)

Three independent layers, all confirmed live or by direct inspection:

1. **LiteLLM's own retry** (`litellm_settings.num_retries: 3`,
   `request_timeout: 60`) - the first line of defense, added after
   hitting a real 429 during this same testing session.
2. **Hermes's own agent-level retry-with-backoff** - already observed
   live in Step 5's evidence (`"Retrying API call in 2.68s (attempt
   1/3)"`) - built into the Hermes Agent project itself, not something
   this project added.
3. **The Dashboard's own graceful failure path** (Step 5) -
   `_dispatch_to_hermes_chief` catches any exception (including an
   eventual exhausted-retries failure) and records `status="failed"` with
   the real error text - never a silent failure, always a visible status
   on the Command page.

## Regression check

Config/new-file changes only (`configs/litellm_config.yaml`,
`configs/litellm_guardrails.py`, `docker-compose.yml`'s litellm volume
mount) - no `core/`/`gateway/`/`dashboard/` Python changed, so the
existing pytest suite is unaffected by this specific change (re-run
alongside the next round of acceptance-test work regardless, to catch
anything unrelated).
