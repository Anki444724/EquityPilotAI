"""OpenRouter provider.

Configuration only. The transport lives in the shape adapter, so this module
carries no request-building or response-parsing logic — that is what makes the
abstraction real rather than nominal.

Phase 1 promoted OpenRouter to the head of the fallback chain: it is the
production writing layer, with Gemini behind it and the deterministic offline
composer behind that. The module gains an `overrides()` hook so the model and
attribution headers come from settings rather than from constants, because
both change without a code change being warranted — see `OPENROUTER_MODEL` in
`app.core.config` for why model availability in particular is deployment data.
"""
from __future__ import annotations

from typing import Any

from app.services.ai.providers.base import ProviderConfig

NAME = "OpenRouter"
PAYLOAD_SHAPE = "openai"

DEFAULTS = ProviderConfig(
    name=NAME,
    endpoint="https://openrouter.ai/api/v1/chat/completions",
    auth_header="Authorization: Bearer {key}",
    payload_shape=PAYLOAD_SHAPE,
    response_path="choices[0].message.content",
    default_model="openai/gpt-4o-mini",
    input_cost_per_m=0.15,
    output_cost_per_m=0.60,
)


def overrides(settings: Any) -> dict[str, Any]:
    """Deployment-supplied fields, merged over `DEFAULTS` by the router.

    Returns a plain mapping rather than a `ProviderConfig` so the router keeps
    sole responsibility for assembling the registry row — including the API
    key, which deliberately never passes through this module.
    """
    return {
        "default_model": (
            getattr(settings, "OPENROUTER_MODEL", None) or DEFAULTS.default_model
        ),
        # OpenRouter attributes usage to a site and app name; unattributed
        # traffic is rate-limited more aggressively and does not appear on the
        # account's activity page, which makes spend impossible to audit.
        "extra_headers": (
            ("HTTP-Referer", getattr(settings, "OPENROUTER_SITE_URL", "") or ""),
            ("X-Title", getattr(settings, "OPENROUTER_APP_NAME", "") or ""),
        ),
    }
