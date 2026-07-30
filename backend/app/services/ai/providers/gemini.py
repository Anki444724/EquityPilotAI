"""Gemini provider.

Configuration only. The transport lives in the shape adapter, so this module
carries no request-building or response-parsing logic — that is what makes the
abstraction real rather than nominal.
"""
from __future__ import annotations

from app.services.ai.providers.base import ProviderConfig

NAME = "Gemini"
PAYLOAD_SHAPE = "gemini"

DEFAULTS = ProviderConfig(
    name=NAME,
    endpoint=(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent?key={key}"
    ),
    auth_header="(key in URL)",
    payload_shape=PAYLOAD_SHAPE,
    response_path="candidates[0].content.parts[0].text",
    # `gemini-1.5-pro` was retired and now 404s on v1beta — the API answers
    # "models/gemini-1.5-pro is not found for API version v1beta". Verified
    # against a live key: of 42 models exposing generateContent, the current
    # general-purpose pair is 2.5-flash and 2.5-pro. Flash is the default
    # because research prompts here are long-context and high-volume, and the
    # quality gap on summarisation does not justify roughly ten times the
    # price per million tokens.
    default_model="gemini-2.0-flash",
    input_cost_per_m=0.10,
    output_cost_per_m=0.40,
)
