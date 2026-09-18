"""Phase 2E — the deferred hole: document-pipeline embeddings are ungated.

Phase 2E A1 closed the LLM provider registry. A2 closed seven more doors,
and `app/services/retrieval/embeddings.py` was one of them: with
`AI_EXTERNAL_PROVIDERS_ENABLED=false`, `build_semantic_embedder` returns
None and `_HTTPEmbeddingProvider.embed` refuses before it reaches the
network.

The document-ingestion pipeline embeds through a *different* module —
`app/services/documents/pipeline/embeddings.py` — and that module was
never brought under the gate. It has its own `build_embedder`, its own
`OpenAIEmbeddingProvider`, and its own transport call:

    build_embedder("openai", api_key=key)   → OpenAIEmbeddingProvider
    OpenAIEmbeddingProvider.embed([...])    → httpx.post(api.openai.com)

Neither consults `external_providers_enabled`. So a deployment that has
turned external AI providers off still makes a paid call to OpenAI the
moment a document is ingested with `OPENAI_API_KEY` configured and the
provider named. The retrieval index is honest and the document index is
not, and nothing in the platform reports the difference.

These are failing-first tests for the fix. Nothing here modifies
production code, and the contract they assert is the one A2 already
established elsewhere so the fix is a repetition rather than an invention:

* **The builder falls back to the local embedder.** `build_embedder`
  already documents falling back as the right behaviour — "an unconfigured
  key must degrade indexing quality, not prevent a user from uploading a
  document". A closed gate is the same situation with a different cause:
  the honest local embedder serves, and ingestion continues. Returning
  None here would break every caller, because unlike
  `build_semantic_embedder` this builder has no None-tolerant contract.
* **The provider refuses before the network.** The builder is the first
  lock; `embed` is the second, for the caller that constructs the provider
  itself — an ingest script, a backfill, a test. Refusal is a
  `RuntimeError` naming the setting, which is what
  `external_gate.gate_detail` already produces and what the ingestion path
  already knows how to survive.
* **Enabled means unchanged.** The flag defaults to true, so the
  interesting regression is not "disabled mode is broken" but "adding the
  gate quietly changed the default path". Both locks are therefore
  exercised in the enabled state against a stubbed transport that returns
  a live-shaped payload, and the local embedder is asserted
  byte-identical across both states.
* **Nothing is removed.** The provider class, its registry entry, the
  httpx dependency and the setting's default all stay exactly where they
  are. Phase 2E isolates; it does not delete.

The gate is read through the helper on purpose. A2 asserts that exactly
two modules dereference `AI_EXTERNAL_PROVIDERS_ENABLED` from a settings
object — the gate helper and A1's registry gate — so this module must not
become a third. `test_the_module_uses_the_gate_helper_not_a_raw_settings_read`
holds that line, and it passes both before and after the fix.
"""
from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings, settings
from app.services.ai.external_gate import SETTING_NAME, gate_detail
from app.services.documents.pipeline import embeddings as pipeline_embeddings
from app.services.documents.pipeline.embeddings import (
    EmbeddingProvider,
    EmbeddingSpec,
    HashingEmbeddingProvider,
    OpenAIEmbeddingProvider,
    available_providers,
    build_embedder,
    cosine,
    stem,
    stem_tokens,
    tokenise,
)

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
MODULE_RELATIVE = "app/services/documents/pipeline/embeddings.py"

#: The prose the pipeline embeds in these tests. Carries a figure and a
#: rupee sign so the local embedder is exercised on realistic text rather
#: than on a token or two.
SAMPLE_TEXT = "Revenue grew 20% year on year to ₹4,210 crore."


# ---------------------------------------------------------------------------
# Flag control
# ---------------------------------------------------------------------------
@pytest.fixture()
def flag(monkeypatch):
    """Set `AI_EXTERNAL_PROVIDERS_ENABLED` on the running singleton.

    Returns a callable so one test can move between states, which several
    need: the honest comparison is the same call on both sides of the
    switch. Mirrors A2's fixture of the same name — `build_embedder` takes
    no settings argument, so the singleton is the only thing it can read.
    """

    def set_flag(enabled: bool) -> bool:
        monkeypatch.setattr(settings, SETTING_NAME, enabled)
        return enabled

    set_flag(True)
    return set_flag


@pytest.fixture()
def api_key() -> str:
    """A key that is present in BOTH flag states.

    Isolation has to hold because of the flag, not because the credential
    happens to be absent — a test that unset the key would pass against
    code with no gate at all.
    """
    return f"sk-test-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Transport sentinels
# ---------------------------------------------------------------------------
class _NetworkAttempted(RuntimeError):
    """Raised by a transport stub, so an attempt is visible as an exception."""


class _HttpxPostSentinel:
    """Stands in for the synchronous `httpx.post` and counts what it was asked.

    Counting rather than merely raising: a test that asserts "no external
    call" must distinguish zero attempts from an attempt that was swallowed
    by a broad `except Exception` somewhere upstream, and only a counter can
    tell those apart.

    `httpx` is imported *inside* `OpenAIEmbeddingProvider.embed` — a
    deliberate property of the production module, because it means a
    disabled deployment never loads the client at all. Patching the
    attribute on the real module is therefore enough to intercept it: the
    deferred `import httpx` binds the patched module object.
    """

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.payload = payload

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_kwargs(self) -> dict:
        return self.calls[-1][1]

    def fake_post(self, url, *args, **kwargs):
        self.calls.append(((url,) + args, kwargs))
        if self.payload is None:
            raise _NetworkAttempted(f"httpx.post reached the network: {url}")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: self.payload,
        )


@pytest.fixture()
def network(monkeypatch):
    """Tripwire. Every sync `httpx.post` raises, and the attempt is counted.

    This is the load-bearing assertion of the file: a provider that is
    unreachable and a provider that was never contacted look identical from
    the response side. Only the transport can tell them apart.
    """
    import httpx

    sentinel = _HttpxPostSentinel()
    monkeypatch.setattr(httpx, "post", sentinel.fake_post)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("urllib.request.urlopen was called"),
    )
    return sentinel


@pytest.fixture()
def openai_wire(monkeypatch):
    """A live-shaped `httpx.post` stub, for the enabled-mode regression.

    Returns rows out of input order on purpose: `embed` documents that the
    API does not promise order and sorts by `index`. A stub that returned
    them in order would let that sort rot unnoticed.
    """
    import httpx

    texts = [SAMPLE_TEXT, "The board met four times during the year."]
    payload = {
        "data": [
            {"index": 1, "embedding": [0.5, -0.5, 0.25]},
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
        ],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 9, "total_tokens": 9},
    }
    sentinel = _HttpxPostSentinel(payload)
    monkeypatch.setattr(httpx, "post", sentinel.fake_post)
    sentinel.texts = texts
    sentinel.expected = [[0.1, 0.2, 0.3], [0.5, -0.5, 0.25]]
    return sentinel


@pytest.fixture()
def no_construction(monkeypatch):
    """Count constructions of `OpenAIEmbeddingProvider`.

    Stronger than asserting the builder's *return type*: a gate that
    constructs the provider and then discards it would pass a type check
    while still running vendor `__init__` code on a disabled deployment.
    """
    built: list[dict] = []
    original = OpenAIEmbeddingProvider.__init__

    def counting_init(self, *args, **kwargs):
        built.append({"args": args, "kwargs": kwargs})
        original(self, *args, **kwargs)

    monkeypatch.setattr(OpenAIEmbeddingProvider, "__init__", counting_init)
    return built


# ===========================================================================
# 1. The builder — a closed gate must not yield the OpenAI provider
# ===========================================================================
class TestBuildEmbedderIsGated:
    def test_a_named_openai_provider_does_not_survive_a_closed_gate(
        self, flag, api_key, network, no_construction,
    ):
        """The hole, stated directly. Today this returns
        `OpenAIEmbeddingProvider` and the deployment that switched external
        providers off embeds its filings through a paid API anyway."""
        flag(False)
        embedder = build_embedder("openai", api_key=api_key)

        assert not isinstance(embedder, OpenAIEmbeddingProvider)
        assert isinstance(embedder, EmbeddingProvider)
        assert no_construction == [], "the vendor provider was constructed"
        assert network.call_count == 0

    def test_a_closed_gate_serves_the_local_embedder_instead(
        self, flag, api_key, network,
    ):
        """Falling back is what this builder already documents for an absent
        key. A closed gate is the same degradation with a different cause:
        ingestion continues on the honest local model."""
        flag(False)
        embedder = build_embedder("openai", api_key=api_key)

        assert isinstance(embedder, HashingEmbeddingProvider)
        vector = embedder.embed_one(SAMPLE_TEXT)
        assert len(vector) == embedder.spec.dimension
        assert any(v != 0.0 for v in vector), "no vector was produced"
        assert network.call_count == 0

    def test_a_bare_provider_name_is_also_gated(self, flag, network, no_construction):
        """`provider="openai"` with no key falls back today, so this case
        cannot distinguish a gate from an accident. It is asserted anyway:
        the fallback must hold in the disabled state for the *stated*
        reason, and it must never construct the vendor provider."""
        flag(False)
        embedder = build_embedder("openai")

        assert not isinstance(embedder, OpenAIEmbeddingProvider)
        assert isinstance(embedder, HashingEmbeddingProvider)
        assert no_construction == []
        assert network.call_count == 0

    def test_an_unknown_provider_name_still_falls_back(self, flag, network):
        flag(False)
        for name in ("cohere", "jina", "", "OPENAI"):
            embedder = build_embedder(name, api_key="k")
            assert not isinstance(embedder, OpenAIEmbeddingProvider), name
            assert isinstance(embedder, HashingEmbeddingProvider), name
        assert network.call_count == 0

    def test_the_default_builder_is_flag_invariant(self, flag, api_key):
        """No provider named means the local embedder, in both states. This
        is the path the orchestrator and the document service take, and it
        must not move."""
        results = {}
        for enabled in (True, False):
            flag(enabled)
            embedder = build_embedder()
            results[enabled] = (
                type(embedder).__name__,
                embedder.spec.key,
                embedder.embed_one(SAMPLE_TEXT),
            )
        assert results[True] == results[False]
        assert results[True][0] == "HashingEmbeddingProvider"

    def test_a_disabled_deployment_embeds_identically_to_an_unkeyed_one(
        self, flag, api_key,
    ):
        """The whole point of the fallback: a deployment that turned the
        providers off and one that never had a key produce the same index,
        so neither silently serves vectors the other would not."""
        flag(True)
        unkeyed = build_embedder("openai", api_key=None).embed_one(SAMPLE_TEXT)
        flag(False)
        disabled = build_embedder("openai", api_key=api_key).embed_one(SAMPLE_TEXT)
        assert disabled == unkeyed


# ===========================================================================
# 2. The second lock — a directly constructed provider refuses, offline
# ===========================================================================
class TestDirectProviderRefusesBeforeTheNetwork:
    def test_embed_refuses_while_disabled(self, flag, api_key, network):
        """The builder is the first lock; this is the second. An ingest
        script, a backfill or a test that constructs the provider itself
        must not reach the network either."""
        flag(False)
        provider = OpenAIEmbeddingProvider(api_key)

        with pytest.raises(RuntimeError) as exc:
            provider.embed([SAMPLE_TEXT])

        assert network.call_count == 0, "httpx.post was reached"

    def test_the_refusal_names_the_setting(self, flag, api_key, network):
        """An operator reading a log has to be able to tell configuration
        from failure. Naming the setting — and only the setting — is what
        `gate_detail` already guarantees on every other gated path."""
        flag(False)
        provider = OpenAIEmbeddingProvider(api_key)

        with pytest.raises(RuntimeError) as exc:
            provider.embed([SAMPLE_TEXT])

        message = str(exc.value)
        assert SETTING_NAME in message
        assert network.call_count == 0

    def test_embed_one_is_refused_too(self, flag, api_key, network):
        """`embed_one` delegates to `embed`, so one lock covers both. It is
        asserted separately because the ingestion pipeline is the caller
        that would use it, and a gate that only covered the batch entry
        point would look complete while leaving the door open."""
        flag(False)
        provider = OpenAIEmbeddingProvider(api_key)

        with pytest.raises(RuntimeError):
            provider.embed_one(SAMPLE_TEXT)

        assert network.call_count == 0

    def test_the_gate_precedes_the_key_check(self, flag, network):
        """Disabled *and* unkeyed must refuse for the gate's reason.

        This is the ordering trap. `embed` opens with
        `if not self.available: raise RuntimeError("... OPENAI_API_KEY")`,
        and a gate added *after* that line reports a missing key on a
        deployment that deliberately turned providers off — sending the
        operator hunting for a credential that is supposed to be unused.
        """
        flag(False)
        provider = OpenAIEmbeddingProvider(None)
        assert provider.available is False

        with pytest.raises(RuntimeError) as exc:
            provider.embed([SAMPLE_TEXT])

        message = str(exc.value)
        assert SETTING_NAME in message, f"wrong reason reported: {message!r}"
        assert "OPENAI_API_KEY" not in message
        assert network.call_count == 0

    def test_the_gate_precedes_the_import_of_the_client(self, flag, api_key, monkeypatch):
        """A disabled deployment must never even load `httpx`.

        The import sits inside `embed` today, which is what makes this
        assertable at all: if the gate is checked first, a run with
        `httpx` made unimportable still refuses cleanly instead of failing
        with an ImportError that says nothing about the flag.
        """
        import builtins

        flag(False)
        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "httpx":
                pytest.fail("httpx was imported on a disabled deployment")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocking_import)

        with pytest.raises(RuntimeError) as exc:
            OpenAIEmbeddingProvider(api_key).embed([SAMPLE_TEXT])
        assert SETTING_NAME in str(exc.value)

    def test_the_refusal_does_not_leak_the_key(self, flag, api_key, network):
        """The message an operator reads ends up in logs and in ingested
        document error rows. It names a setting; it must not echo a secret."""
        flag(False)
        with pytest.raises(RuntimeError) as exc:
            OpenAIEmbeddingProvider(api_key).embed([SAMPLE_TEXT])

        assert api_key not in str(exc.value)
        assert network.call_count == 0

    def test_the_refusal_is_the_wording_the_gate_helper_already_produces(
        self, flag, api_key, network,
    ):
        """Consistency with the seven paths A2 closed. `gate_detail` is the
        platform's one honest sentence for this situation, and a second,
        independently worded refusal is a second thing to keep correct."""
        flag(False)
        with pytest.raises(RuntimeError) as exc:
            OpenAIEmbeddingProvider(api_key).embed([SAMPLE_TEXT])

        message = str(exc.value)
        assert "declined" in message
        assert f"{SETTING_NAME}=false" in message
        assert message == gate_detail(f"{OpenAIEmbeddingProvider.name} embeddings")
        assert network.call_count == 0

    def test_the_instance_still_reports_its_own_availability(self, flag, api_key):
        """`available` answers "is a key configured", and the gate does not
        unset the key — A2's whole reversibility argument. A disabled
        deployment still reports the credential it holds."""
        flag(False)
        assert OpenAIEmbeddingProvider(api_key).available is True
        assert OpenAIEmbeddingProvider(None).available is False


# ===========================================================================
# 3. Enabled mode — the existing OpenAI behaviour is untouched
# ===========================================================================
class TestEnabledModeIsUnchanged:
    def test_the_builder_still_returns_the_openai_provider(self, flag, api_key, no_construction):
        flag(True)
        embedder = build_embedder("openai", api_key=api_key)

        assert isinstance(embedder, OpenAIEmbeddingProvider)
        assert len(no_construction) == 1
        assert embedder.api_key == api_key

    def test_the_selected_spec_is_the_one_it_has_always_been(self, flag, api_key):
        flag(True)
        embedder = build_embedder("openai", api_key=api_key)

        assert embedder.spec == EmbeddingSpec(
            "openai", "text-embedding-3-small", 1536,
        )
        assert embedder.spec.key == "openai:text-embedding-3-small:1536"
        assert embedder.available is True

    def test_embed_still_reaches_the_transport(self, flag, api_key, openai_wire):
        """The control for every disabled-mode assertion above: with the
        flag on, the same call reaches `httpx.post`. Without this a gate
        that simply broke the provider would look like a pass."""
        flag(True)
        provider = OpenAIEmbeddingProvider(api_key)

        vectors = provider.embed(openai_wire.texts)

        assert openai_wire.call_count == 1
        assert vectors == openai_wire.expected

    def test_the_request_shape_is_unchanged(self, flag, api_key, openai_wire):
        flag(True)
        OpenAIEmbeddingProvider(api_key).embed(openai_wire.texts)

        url = openai_wire.calls[0][0][0]
        kwargs = openai_wire.last_kwargs
        assert url == OpenAIEmbeddingProvider.ENDPOINT
        assert url == "https://api.openai.com/v1/embeddings"
        assert kwargs["headers"]["Authorization"] == f"Bearer {api_key}"
        assert kwargs["json"] == {
            "model": "text-embedding-3-small",
            "input": list(openai_wire.texts),
        }
        assert kwargs["timeout"] == 30.0

    def test_the_response_is_reordered_by_index(self, flag, api_key, openai_wire):
        """The stub returns rows out of order; `embed` sorts them. Asserted
        explicitly because the enabled-mode regression is about *behaviour*,
        not merely about a call happening."""
        flag(True)
        vectors = OpenAIEmbeddingProvider(api_key).embed(openai_wire.texts)

        assert vectors[0] == [0.1, 0.2, 0.3]
        assert vectors[1] == [0.5, -0.5, 0.25]

    def test_a_missing_key_still_reports_the_missing_key(self, flag, network):
        """The pre-existing failure message, in the state where it has
        always applied. The gate adds a refusal; it does not reword this."""
        flag(True)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            OpenAIEmbeddingProvider(None).embed([SAMPLE_TEXT])
        assert network.call_count == 0

    def test_an_unkeyed_builder_is_flag_invariant(self, flag):
        for enabled in (True, False):
            flag(enabled)
            embedder = build_embedder("openai", api_key=None)
            assert isinstance(embedder, HashingEmbeddingProvider), enabled

    def test_a_bad_response_still_raises_from_the_transport_layer(
        self, flag, api_key, monkeypatch,
    ):
        """`raise_for_status` is the provider's existing contract with a
        failing API. The gate must not swallow it."""
        import httpx

        flag(True)

        def exploding_post(*args, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: pytest.fail("boom"),
                json=lambda: {},
            )

        raised = []
        monkeypatch.setattr(
            httpx, "post",
            lambda *a, **k: raised.append(k) or exploding_post(*a, **k),
        )
        with pytest.raises(BaseException, match="boom"):
            OpenAIEmbeddingProvider(api_key).embed([SAMPLE_TEXT])
        assert raised, "the transport was never reached"


# ===========================================================================
# 4. The local embedder and the shared helpers are flag-invariant
# ===========================================================================
class TestLocalEmbeddingIsFlagInvariant:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_local_provider_is_identical_in_both_states(self, flag, enabled):
        flag(enabled)
        provider = HashingEmbeddingProvider()
        assert provider.spec == EmbeddingSpec("local-hashing", "hash-4g", 384)
        assert provider.embed_one(SAMPLE_TEXT) == HashingEmbeddingProvider().embed_one(
            SAMPLE_TEXT,
        )

    def test_the_helpers_are_untouched_by_the_flag(self, flag):
        """`tokenise`, `stem` and `cosine` are shared with the lexical index.
        Nothing about this fix may reach them."""
        outputs = {}
        for enabled in (True, False):
            flag(enabled)
            outputs[enabled] = {
                "tokens": tokenise(SAMPLE_TEXT),
                "stems": stem_tokens("competitors guide acquisition guidance"),
                "stem_fixed_point": stem(stem("competition")) == stem("competition"),
                "cosine_self": cosine([1.0, 0.0], [1.0, 0.0]),
                "cosine_orthogonal": cosine([1.0, 0.0], [0.0, 1.0]),
                "cosine_mismatched": cosine([1.0], [1.0, 2.0]),
            }
        assert outputs[True] == outputs[False]

    def test_the_orchestrator_default_is_flag_invariant(self, flag):
        """The ingestion pipeline and the document service both default to
        `HashingEmbeddingProvider()` directly, never through the builder.
        That default is local in both states and is not this fix's to move."""
        from app.services.documents.pipeline.orchestrator import IngestionPipeline

        defaults = {}
        for enabled in (True, False):
            flag(enabled)
            pipeline = IngestionPipeline()
            defaults[enabled] = (
                type(pipeline.embedder).__name__,
                pipeline.embedder.spec.key,
            )
        assert defaults[True] == defaults[False]
        assert defaults[True][0] == "HashingEmbeddingProvider"

    def test_no_vector_is_fabricated_when_the_gate_is_closed(self, flag, api_key, network):
        """A disabled deployment serves *computed* vectors from the local
        model — never invented ones, and never ones it fetched. The
        distinction is the reason the fallback is the hashed embedder
        rather than a stub that returns zeroes."""
        flag(False)
        embedder = build_embedder("openai", api_key=api_key)

        first = embedder.embed_one(SAMPLE_TEXT)
        second = embedder.embed_one(SAMPLE_TEXT)
        other = embedder.embed_one("A completely different sentence about debt.")

        assert first == second, "the local embedder is deterministic"
        assert first != other, "the vectors carry the text, not a constant"
        assert not all(v == 0.0 for v in first)
        assert cosine(first, first) == pytest.approx(1.0)
        assert network.call_count == 0


# ===========================================================================
# 5. Architecture — nothing is deleted, the gate is structural
# ===========================================================================
class TestArchitecture:
    def test_the_provider_class_and_its_registry_entry_survive(self):
        """Phase 2E is staged. Isolating a path is not removing it."""
        assert OpenAIEmbeddingProvider.name == "openai"
        assert OpenAIEmbeddingProvider.ENDPOINT == (
            "https://api.openai.com/v1/embeddings"
        )
        assert pipeline_embeddings._PROVIDERS == {  # noqa: SLF001
            "local-hashing": HashingEmbeddingProvider,
            "openai": OpenAIEmbeddingProvider,
        }
        assert available_providers() == ("local-hashing", "openai")

    def test_the_provider_defaults_are_unchanged(self):
        provider = OpenAIEmbeddingProvider("k")
        assert provider.model == "text-embedding-3-small"
        assert provider.spec.dimension == 1536
        assert provider.timeout == 30.0
        assert HashingEmbeddingProvider().dimension == 384
        assert HashingEmbeddingProvider().char_ngram == 4

    def test_the_httpx_dependency_is_still_declared(self):
        assert "httpx" in (BACKEND / "requirements.txt").read_text()

    def test_the_setting_default_is_still_true(self):
        """Failing closed on an absent setting would turn a benign
        configuration gap into a platform-wide outage. A2's argument, restated
        for the module this fix touches."""
        assert Settings.model_fields[SETTING_NAME].default is True

    def test_the_env_example_still_documents_the_flag_as_true(self):
        assert f"{SETTING_NAME}=true" in (BACKEND / ".env.example").read_text()

    def test_the_gate_helper_this_module_must_use_is_intact(self):
        gate = APP / "services" / "ai" / "external_gate.py"
        source = gate.read_text()
        assert 'SETTING_NAME = "AI_EXTERNAL_PROVIDERS_ENABLED"' in source
        assert "def external_providers_enabled(" in source
        assert "def gate_detail(" in source
        assert "class ExternalProvidersDisabled(RuntimeError)" in source

    @pytest.mark.parametrize("func", [
        build_embedder,
        OpenAIEmbeddingProvider.embed,
    ])
    def test_each_scoped_callable_consults_the_gate(self, func):
        """The same source-level tripwire A2 lays across its eight paths: a
        future edit that removes a gate should fail here with the name of the
        callable it opened, rather than passing silently until a disabled
        deployment makes a paid call."""
        assert "external_providers_enabled" in inspect.getsource(func), func

    def test_the_module_imports_the_gate_helper(self):
        source = (BACKEND / MODULE_RELATIVE).read_text()
        assert "from app.services.ai.external_gate import" in source
        assert "external_providers_enabled" in source

    def test_the_module_uses_the_gate_helper_not_a_raw_settings_read(self):
        """A2 asserts that exactly two modules dereference this setting from
        a settings object — the gate helper and A1's registry gate — because
        every extra read is another place the safe default can be typo'd into
        its inverse. This fix must consult the helper, never the attribute.
        Passes before and after: it constrains *how* the gate is added."""
        source = (BACKEND / MODULE_RELATIVE).read_text()
        assert f"settings.{SETTING_NAME}" not in source
        assert f'getattr(settings, "{SETTING_NAME}"' not in source
        assert "getattr(settings, SETTING_NAME" not in source

    def test_the_gate_precedes_the_vendor_construction_in_the_builder(self):
        """Ordering, not mere presence. A gate checked after
        `OpenAIEmbeddingProvider(api_key)` is built has already run vendor
        code on a disabled deployment."""
        source = inspect.getsource(build_embedder)
        lines = source.splitlines()

        gate_line = next(
            (i for i, line in enumerate(lines)
             if "external_providers_enabled" in line),
            None,
        )
        build_line = next(
            (i for i, line in enumerate(lines)
             if "OpenAIEmbeddingProvider(" in line),
            None,
        )
        assert gate_line is not None, "the builder never consults the gate"
        assert build_line is not None, "the builder no longer names the provider"
        assert gate_line < build_line

    def test_the_retrieval_gate_a2_shipped_is_untouched(self):
        """The sibling module A2 already closed. This fix adds a gate; it
        does not relocate or rewrite the one that exists."""
        source = (APP / "services" / "retrieval" / "embeddings.py").read_text()
        assert "if not external_providers_enabled(settings):" in source
        assert "def build_semantic_embedder(" in source

    def test_a1_and_a2_isolation_suites_are_still_present(self):
        for name in (
            "test_external_provider_isolation.py",
            "test_external_provider_isolation_a2.py",
        ):
            assert (BACKEND / "tests" / name).is_file(), name
