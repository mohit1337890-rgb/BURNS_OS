"""
LiteLLM custom pre-call hook (Mohit's approval condition 4, free-model
safety): OpenRouter's free-tier endpoints may log/train on submitted
prompts (an opt-out is provider-account-side, not something this proxy
can force) - any model tagged data_class: public_only in
configs/litellm_config.yaml (currently "premium"/"cheap" - both point at
$0 OpenRouter models, see that file's own comments) must never receive
content that looks private/client-identifying/trading-strategy-related.

This is a hard, code-level pre-call block, not a system-prompt
instruction asking a model to be careful - consistent with this whole
project's design rule (permissions enforced in code, not prompts) applied
everywhere else (core/policy_engine.py, core/dlp.py, ...).

Deliberately a coarse keyword/pattern scan, not a full DLP pass - the
question here isn't "does this look like a leaked secret" (core/dlp.py
already owns that, for outgoing plugin content specifically), it's "does
this look like it's about to send Burns Worldwide's own business data to
a third party whose retention/training policy is unknown."
"""

from __future__ import annotations

import re

from litellm.integrations.custom_logger import CustomLogger

# Model names this project's own config.yaml ever points at a $0 free
# route - see configs/litellm_config.yaml's own comments for why both are
# currently public_only. A model_name outside this set is not touched by
# this hook at all (paid/private routes are not "safer" here - they're
# simply out of scope for this specific free-tier-leakage concern).
_PUBLIC_ONLY_MODEL_NAMES = {"premium", "cheap"}

_SENSITIVE_PATTERNS = [
    re.compile(r"\bclient\b", re.IGNORECASE),
    re.compile(r"\btrading[\s_-]?strategy\b", re.IGNORECASE),
    re.compile(r"\bconfidential\b", re.IGNORECASE),
    re.compile(r"\bproprietary\b", re.IGNORECASE),
    re.compile(r"\bprivate\b", re.IGNORECASE),
    re.compile(r"\bdo not share\b", re.IGNORECASE),
    re.compile(r"\bburns worldwide\b", re.IGNORECASE),
]


def _extract_text(messages: list) -> str:
    parts = []
    for m in messages or []:
        content = m.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):  # some content blocks are structured (text/image parts)
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return " ".join(parts)


class PublicOnlyRouteGuardrail(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        model_name = data.get("model", "")
        if model_name not in _PUBLIC_ONLY_MODEL_NAMES:
            return data

        text = _extract_text(data.get("messages", []))
        for pattern in _SENSITIVE_PATTERNS:
            match = pattern.search(text)
            if match:
                # ValueError is LiteLLM's own documented way for a
                # pre_call_hook to block a request (docs.litellm.ai/docs/
                # proxy/call_hooks) - it's surfaced to the caller as a 400.
                raise ValueError(
                    f"Refused by PublicOnlyRouteGuardrail: model {model_name!r} is a public-data-only free "
                    f"route (OpenRouter free tier - retention/training policy unknown) and this request matched "
                    f"a sensitive-data pattern ({match.group(0)!r}). Use a non-free route for private/client/"
                    "trading-strategy content. See docs/MILESTONE_2_HERMES_DESIGN.md."
                )
        return data


public_only_route_guardrail = PublicOnlyRouteGuardrail()
