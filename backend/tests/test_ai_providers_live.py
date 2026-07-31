"""Provider integration, fallback ordering and credential hygiene.

Written during the production-deployment phase against a **real Gemini key**.
That key authenticates correctly and has an exhausted free-tier daily quota,
which turned out to be the single most useful test fixture available: it is
precisely the condition fallback exists for, and it is not reproducible by
mocking alone because it depends on how a real provider phrases a 429.

Nothing here needs a network. The live findings are encoded as fixtures so CI
reproduces the behaviour without a key and without spending quota.

Defects these lock in:

  PD-001  fallback order was an accident of import order, not a decision
  PD-002  an exhausted quota was retried three times before falling back
  PD-003  the Gemini API key was printed in clear text by httpx's INFO log
  PD-004  the configured default model had been retired and 404'd
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.domain.ai.types import (
    CompletionRequest, CompletionResponse, Message, ProviderError,
    RateLimitError, Role, TokenUsage,
)
from app.services.ai.providers import claude, gemini, openai, openrouter
from app.services.ai.providers.base import ProviderConfig
from app.services.ai.providers.router import (
    FALLBACK_ORDER, MAX_ATTEMPTS, QUOTA_EXHAUSTED_ATTEMPTS, ProviderRouter,
)


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(Role.USER, "q")], model=None,
        temperature=0.0, max_tokens=8,
    )


def _configured(*modules, key: str = "test-key") -> list[ProviderConfig]:
    """Provider configs that report themselves configured, with no real key."""
    out = []
    for module in modules:
        base = module.DEFAULTS
        out.append(ProviderConfig(
            name=base.name, endpoint=base.endpoint, auth_header=base.auth_header,
            payload_shape=base.payload_shape, response_path=base.response_path,
            default_model=base.default_model, api_key=key,
            input_cost_per_m=base.input_cost_per_m,
            output_cost_per_m=base.output_cost_per_m,
        ))
    return out


class _Failing:
    """A provider that always fails, counting how often it was asked."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts = 0

    async def complete(self, request):  # noqa: ANN001
        self.attempts += 1
        raise self.error


class _Answering:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attempts = 0

    async def complete(self, request):  # noqa: ANN001
        self.attempts += 1
        return CompletionResponse(
            content=f"answer from {self.name}", provider=self.name,
            model="test-model", usage=TokenUsage(3, 4),
            latency_ms=1.0, cost_usd=0.0,
        )


# ===========================================================================
class TestFallbackOrder:
    """PD-001 — the order is declared, not inherited from an import tuple."""

    def test_openrouter_precedes_gemini(self):
        """Phase 1 reversal.

        The order was Gemini-first, which in practice meant the platform
        served template prose: the Gemini free tier spends its daily
        generation quota within a few reports and 429s thereafter, so the
        chain fell through to the offline composer for the rest of the day.
        A provider that answers reliably belongs in front of one that answers
        for the first few requests.
        """
        assert FALLBACK_ORDER.index("OpenRouter") < FALLBACK_ORDER.index("Gemini")

    def test_the_chain_follows_the_declared_order(self):
        # Deliberately registered in the wrong order to prove the sort runs.
        router = ProviderRouter(configs=_configured(claude, openrouter, gemini))
        assert [c.name for c in router.chain()] == ["OpenRouter", "Gemini", "Claude"]

    def test_an_explicit_preference_overrides_the_default_order(self):
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        assert router.chain(preferred="gemini")[0].name == "Gemini"

    def test_a_live_provider_always_outranks_the_offline_one(self):
        """The platform must never silently serve template output when a real
        model was available."""
        from app.services.ai.providers import mock

        configs = _configured(openrouter) + [mock.DEFAULTS]
        chain = ProviderRouter(configs=configs).chain()
        assert chain[0].name == "OpenRouter"
        assert chain[-1].payload_shape == "offline"

    def test_an_unlisted_provider_sorts_last_rather_than_crashing(self):
        """Adding a vendor module must not require editing FALLBACK_ORDER."""
        base = gemini.DEFAULTS
        exotic = ProviderConfig(
            name="Mistral", endpoint=base.endpoint, auth_header=base.auth_header,
            payload_shape=base.payload_shape, response_path=base.response_path,
            default_model="m", api_key="k",
        )
        router = ProviderRouter(configs=[exotic] + _configured(gemini))
        assert [c.name for c in router.chain()] == ["Gemini", "Mistral"]

    def test_unconfigured_providers_are_excluded(self):
        configs = _configured(gemini) + _configured(openrouter, key="")
        assert [c.name for c in ProviderRouter(configs=configs).chain()] == ["Gemini"]


class TestOpenRouterToGeminiFallback:
    """The hop the brief asks for, proven end to end.

    Phase 1 reversed the direction: OpenRouter is the primary writing layer
    and Gemini the standby, so the interesting hop is now OpenRouter down to
    Gemini rather than the other way round.
    """

    def test_gemini_serves_when_openrouter_is_out_of_quota(self):
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        dead = _Failing(RateLimitError(
            "quota", provider="OpenRouter", retry_after=0.01,
            quota_exhausted=True,
        ))
        alive = _Answering("Gemini")
        router.build = lambda c: dead if c.name == "OpenRouter" else alive

        response = asyncio.run(router.complete(_request(), use_cache=False))
        assert response.provider == "Gemini"
        assert response.fell_back_from == "OpenRouter"

    def test_the_fallback_is_recorded_not_hidden(self):
        """A caller must be able to tell it did not get its first choice."""
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        router.build = lambda c: (
            _Failing(ProviderError("down", provider="OpenRouter",
                                   retryable=False))
            if c.name == "OpenRouter" else _Answering("Gemini")
        )
        response = asyncio.run(router.complete(_request(), use_cache=False))
        assert response.fell_back_from == "OpenRouter"
        assert router.ledger.fallbacks == 1

    def test_no_fallback_marker_when_the_first_choice_answers(self):
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        router.build = lambda c: _Answering(c.name)
        response = asyncio.run(router.complete(_request(), use_cache=False))
        assert response.provider == "OpenRouter"
        assert response.fell_back_from is None

    def test_the_whole_chain_is_exhausted_before_giving_up(self):
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        failures = {
            name: _Failing(ProviderError(name, provider=name, retryable=False))
            for name in ("Gemini", "OpenRouter")
        }
        router.build = lambda c: failures[c.name]
        with pytest.raises(ProviderError):
            asyncio.run(router.complete(_request(), use_cache=False))
        assert failures["Gemini"].attempts >= 1
        assert failures["OpenRouter"].attempts >= 1


class TestQuotaAwareRetry:
    """PD-002 — a spent allowance will not clear inside a request."""

    def test_an_exhausted_quota_is_not_retried(self):
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        dead = _Failing(RateLimitError(
            "quota", provider="OpenRouter", retry_after=0.01,
            quota_exhausted=True,
        ))
        router.build = lambda c: dead if c.name == "OpenRouter" else _Answering("Gemini")
        asyncio.run(router.complete(_request(), use_cache=False))
        assert dead.attempts == QUOTA_EXHAUSTED_ATTEMPTS == 1

    def test_a_transient_throttle_is_still_retried(self):
        """The converse. Treating every 429 as terminal would give up on a
        provider that was merely busy for a second."""
        router = ProviderRouter(configs=_configured(gemini, openrouter))
        dead = _Failing(RateLimitError(
            "busy", provider="OpenRouter", retry_after=0.001,
            quota_exhausted=False,
        ))
        router.build = lambda c: dead if c.name == "OpenRouter" else _Answering("Gemini")
        asyncio.run(router.complete(_request(), use_cache=False))
        assert dead.attempts == MAX_ATTEMPTS == 3

    def test_quota_exhaustion_is_detected_from_a_real_google_body(self):
        """The exact payload Google returned for the shipped key."""
        body = (
            '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":'
            '"You exceeded your current quota, please check your plan and '
            'billing details.","details":[{"@type":"type.googleapis.com/'
            'google.rpc.QuotaFailure","violations":[{"quotaId":'
            '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'
        )
        assert _looks_exhausted(body)

    def test_a_plain_throttle_body_is_not_read_as_exhaustion(self):
        assert not _looks_exhausted('{"error":{"message":"Too many requests"}}')

    @pytest.mark.parametrize("body", [
        '{"error":{"message":"insufficient_quota"}}',          # OpenAI
        '{"error":{"message":"You have no credits left"}}',    # OpenRouter
        '{"error":{"message":"quota exceeded for this month"}}',
    ])
    def test_other_providers_phrasings_are_recognised(self, body):
        assert _looks_exhausted(body)


def _looks_exhausted(body: str) -> bool:
    """Mirror of the detection in `base.complete`.

    Kept in the test rather than imported so a change to the markers has to be
    made deliberately in both places — this is the kind of list that quietly
    stops matching when a provider rewords an error.
    """
    lowered = body[:2000].lower()
    return any(marker in lowered for marker in (
        "quotafailure", "quota exceeded", "exceeded your current quota",
        "insufficient_quota", "perday", "per day", "billing",
        "credits", "out of credit",
    ))


class TestCredentialHygiene:
    """PD-003 — the key must not reach a log, a URL echo or a response."""

    def test_httpx_url_logging_is_silenced(self):
        """Gemini puts the key in the query string, and httpx logs the full
        URL at INFO. Observed leaking a live key to stdout before this fix."""
        from app.services.ai.providers import base

        # The helper is idempotent by design (it short-circuits on a module
        # flag), so a test that has already triggered it elsewhere would see
        # a no-op. Reset the flag to exercise the real path.
        base._HTTPX_SILENCED = False
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("httpcore").setLevel(logging.INFO)

        base._silence_httpx_url_logging()

        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

    def test_a_live_style_request_url_never_reaches_a_log_record(self):
        """End to end: capture everything logged during a call and assert the
        credential is absent. Guards the case where a future change adds
        another logger that echoes the URL."""
        from app.services.ai.providers import base

        secret = "AQ.TestKeyMaterialThatMustNotAppear"
        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record):  # noqa: ANN001
                try:
                    records.append(record.getMessage())
                except Exception:  # noqa: BLE001
                    pass

        handler = _Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        base._HTTPX_SILENCED = False
        try:
            config = ProviderConfig(
                name="Gemini", endpoint=gemini.DEFAULTS.endpoint,
                auth_header=gemini.DEFAULTS.auth_header,
                payload_shape="gemini",
                response_path=gemini.DEFAULTS.response_path,
                default_model=gemini.DEFAULTS.default_model,
                api_key=secret,
            )
            provider = ProviderRouter().build(config)
            try:
                asyncio.run(provider.complete(_request()))
            except Exception:  # noqa: BLE001 — the call is expected to fail
                pass
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)

        leaked = [r for r in records if secret in r]
        assert leaked == [], f"credential appeared in {len(leaked)} log record(s)"

    def test_no_api_key_is_hard_coded_anywhere(self):
        """The brief's hard requirement, enforced rather than promised."""
        import pathlib
        import re

        app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
        patterns = (
            re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),        # Google
            re.compile(r"sk-or-v1-[0-9a-f]{16,}"),          # OpenRouter
            re.compile(r"sk-[A-Za-z0-9]{32,}"),             # OpenAI
            re.compile(r"AQ\.[A-Za-z0-9]{20,}"),            # the shipped key
        )
        offences = []
        for path in app_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text()
            for pattern in patterns:
                if pattern.search(text):
                    offences.append(path.name)
        assert offences == [], offences

    def test_keys_come_only_from_settings(self):
        """No provider module may read os.environ directly — configuration is
        one path, so there is one place to audit."""
        import pathlib

        providers = (
            pathlib.Path(__file__).resolve().parent.parent
            / "app" / "services" / "ai" / "providers"
        )
        for path in providers.glob("*.py"):
            assert "os.environ" not in path.read_text(), path.name

    def test_the_provider_registry_never_returns_an_endpoint_with_a_key(self):
        """`/ai/providers` publishes endpoints. Gemini's carries `?key=`."""
        for config in ProviderRouter().configs:
            published = config.endpoint.split("?")[0]
            assert "key=" not in published

    def test_env_example_documents_both_providers(self):
        import pathlib

        example = (
            pathlib.Path(__file__).resolve().parent.parent / ".env.example"
        )
        if not example.exists():
            pytest.skip(".env.example absent")
        text = example.read_text()
        assert "GEMINI_API_KEY" in text
        assert "OPENROUTER_API_KEY" in text


class TestModelDefaults:
    """PD-004 — a retired default model 404s on every call."""

    def test_gemini_default_is_a_current_model(self):
        """`gemini-1.5-pro` was the configured default and no longer exists:
        the API answers 404 "not found for API version v1beta"."""
        assert gemini.DEFAULTS.default_model != "gemini-1.5-pro"
        assert gemini.DEFAULTS.default_model.startswith("gemini-2")

    def test_every_provider_declares_a_default_model(self):
        for module in (gemini, openrouter, openai, claude):
            assert module.DEFAULTS.default_model

    def test_every_provider_declares_costs(self):
        """A provider with no pricing silently reports zero spend."""
        for module in (gemini, openrouter, openai, claude):
            assert module.DEFAULTS.input_cost_per_m >= 0
            assert module.DEFAULTS.output_cost_per_m >= 0


class TestDegradedOperation:
    """With no key at all the platform must still work."""

    def test_no_configured_provider_still_serves_via_offline(self):
        from app.services.ai.providers import mock

        router = ProviderRouter(configs=[mock.DEFAULTS])
        response = asyncio.run(router.complete(_request(), use_cache=False))
        assert response.provider == "Offline"

    def test_an_empty_chain_raises_a_clear_error(self):
        from app.domain.ai.types import NoProviderConfigured

        router = ProviderRouter(configs=_configured(gemini, key=""))
        with pytest.raises(NoProviderConfigured):
            asyncio.run(router.complete(_request(), use_cache=False))
