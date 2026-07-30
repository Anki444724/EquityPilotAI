"""Claude provider.

Configuration only. The transport lives in the shape adapter, so this module
carries no request-building or response-parsing logic — that is what makes the
abstraction real rather than nominal.
"""
from __future__ import annotations

from app.services.ai.providers.base import ProviderConfig

NAME = "Claude"
PAYLOAD_SHAPE = "anthropic"

DEFAULTS = ProviderConfig(
    name=NAME,
    endpoint="https://api.anthropic.com/v1/messages",
    auth_header="x-api-key: {key}|anthropic-version: 2023-06-01",
    payload_shape=PAYLOAD_SHAPE,
    response_path="content[0].text",
    default_model="claude-3-5-sonnet-20241022",
    input_cost_per_m=3.00,
    output_cost_per_m=15.00,
)
