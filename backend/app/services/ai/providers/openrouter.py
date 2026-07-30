"""OpenRouter provider.

Configuration only. The transport lives in the shape adapter, so this module
carries no request-building or response-parsing logic — that is what makes the
abstraction real rather than nominal.
"""
from __future__ import annotations

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
