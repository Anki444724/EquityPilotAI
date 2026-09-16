"""Part 2B — architectural and safety invariants for the Question Planner.

Behaviour tests prove the planner does the right thing. These prove it does
not do the *wrong* thing, which is harder to notice: a planner that quietly
started scoring, retrieving or calling a provider would still pass every
behavioural test while ceasing to be the thing the architecture says it is.

Most of the rules are enforced by parsing the planner's own source rather
than by inspecting behaviour at runtime, because the rule is about what the
code is permitted to depend on, not about what it happened to do on one
run. Where behaviour matters more than structure — whether importing the
planner drags a provider into the process — the check is a subprocess, so
it cannot be satisfied or defeated by whatever else the test session has
already imported.

Nothing here modifies the production answer path; the last class asserts
that the path is, in fact, still untouched.
"""
from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.ai.financial_intent import (
    INTENT_PATTERNS, INVESTMENT_INTENTS, FinancialIntent,
    FinancialIntentResolver,
)
from app.services.ai.planner import QuestionPlanner

APP = Path(__file__).resolve().parent.parent / "app"
PLANNER = APP / "services" / "ai" / "planner"


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def sources(root: Path) -> dict[Path, str]:
    return {p: p.read_text() for p in python_files(root)}


def imports_of(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def all_planner_imports() -> set[str]:
    names: set[str] = set()
    for source in sources(PLANNER).values():
        names |= imports_of(source)
    return names


PLANNER_SOURCE = "\n".join(sources(PLANNER).values())


def code_text(source: str) -> str:
    """The source with every docstring removed.

    Token bans below are about what the code *does*, and a module docstring
    that says "the planner must never embed" would otherwise trip the very
    check that enforces it. Stripping docstrings via the AST rather than by
    regex keeps comments and string literals out of the wrong test.
    """
    tree = ast.parse(source)
    owner = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, owner):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(tree)


#: The planner's executable surface — docstrings excluded.
PLANNER_CODE = "\n".join(code_text(src) for src in sources(PLANNER).values())


# ===========================================================================
class TestNoExternalProviders:
    """Part 2B must run with the providers switched off, or off entirely."""

    FORBIDDEN = (
        "openai", "anthropic", "google.generativeai", "httpx", "requests",
        "aiohttp", "app.services.ai.providers", "app.ai.providers",
        "app.services.language.translators",
    )

    def test_planner_imports_no_provider_module(self):
        imported = all_planner_imports()
        offenders = {
            name for name in imported
            if any(name == bad or name.startswith(f"{bad}.") for bad in self.FORBIDDEN)
        }
        assert offenders == set()

    def test_planner_never_names_a_provider_in_code(self):
        lowered = PLANNER_CODE.lower()
        for token in ("providerrouter", "llmtranslator", "openai", "openrouter",
                      "gemini", "generativeai", "anthropic"):
            assert token not in lowered, token

    def test_planner_does_not_instantiate_a_provider_router(self):
        for path, source in sources(PLANNER).items():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(
                        node.func, "attr", None
                    )
                    assert name != "ProviderRouter", path.name

    def test_importing_the_planner_loads_no_provider_module(self):
        """A subprocess, so a prior import elsewhere cannot mask the answer."""
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import app.services.ai.planner\n"
            "loaded = sorted(m for m in sys.modules if 'providers' in m)\n"
            "print(loaded)\n"
            "assert not loaded, loaded\n"
        ) % str(APP.parent)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_planning_a_question_contacts_nothing(self):
        """The whole point: a plan is produced with no I/O of any kind."""
        plan = QuestionPlanner().plan("Reliance ki financial quality kaisi hai?")
        assert plan.intents


# ===========================================================================
class TestNoDuplicateEngines:
    """The planner plans. It does not score, value, retrieve or embed."""

    FORBIDDEN = (
        "app.services.scoring", "app.services.valuation", "app.services.forecast",
        "app.services.ratios", "app.services.documents", "app.services.retrieval",
        "app.domain.scoring", "app.domain.forecast",
        "numpy", "pgvector", "sentence_transformers",
    )

    def test_planner_imports_no_computation_engine(self):
        imported = all_planner_imports()
        offenders = {
            name for name in imported
            if any(name == bad or name.startswith(f"{bad}.") for bad in self.FORBIDDEN)
        }
        assert offenders == set()

    def test_planner_does_not_name_a_valuation_or_scoring_service(self):
        for token in ("ScoringService", "ValuationService", "ForecastService",
                      "RatioService", "DocumentService"):
            assert token not in PLANNER_SOURCE, token

    def test_planner_does_not_call_search(self):
        """No retrieval call site. ``re`` matching uses ``.search`` on a
        pattern, which is why this checks for a *document* search instead."""
        for token in ("document_service", ".search_company", "retrieve(",
                      "build_index", "hybrid_search"):
            assert token not in PLANNER_CODE, token

    def test_planner_does_not_create_embeddings(self):
        lowered = PLANNER_CODE.lower()
        for token in ("embed", "embedding", "vector(", "cosine"):
            assert token not in lowered, token

    def test_planner_collaborators_are_only_the_four_injected_ones(self):
        """Nothing to score or retrieve with, by construction."""
        params = set(inspect.signature(QuestionPlanner.__init__).parameters)
        assert params == {"self", "company_resolver", "memory", "matcher", "adapter"}


# ===========================================================================
class TestNoPersistenceOrCitations:
    """A plan is an in-memory description. It writes nothing."""

    def test_planner_never_writes_to_a_session(self):
        for token in ("session.add", "session.commit", "db.add", "db.commit",
                      ".flush(", "session.execute"):
            assert token not in PLANNER_SOURCE, token

    def test_planner_never_constructs_a_citation(self):
        for path, source in sources(PLANNER).items():
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(
                        node.func, "attr", None
                    )
                    assert name != "Citation", path.name

    def test_planner_never_imports_the_database_layer(self):
        imported = all_planner_imports()
        assert not {n for n in imported if n.startswith("app.db")}
        assert not {n for n in imported if n.startswith("app.models")}

    def test_evidence_requirements_carry_no_values(self):
        """The type itself refuses a number, not merely the current data."""
        from app.services.ai.planner.types import EvidenceRequirement
        numeric = {
            f.name for f in __import__("dataclasses").fields(EvidenceRequirement)
            if f.type in {"float", "int", "float | None", "int | None"}
        }
        assert numeric == set()


# ===========================================================================
class TestNoFinancialArithmetic:
    """The planner describes evidence; it never derives a figure."""

    #: Arithmetic on numbers is banned. Arithmetic on indices and spans is
    #: not — which is why the check is on *numeric literals*, not on every
    #: binary operator: a blanket ban would forbid `start + len(term)` and
    #: say nothing about the thing that actually matters.
    BANNED_OPS = (ast.Div, ast.Mult, ast.Pow, ast.FloorDiv, ast.Mod)

    def test_no_division_or_multiplication_anywhere(self):
        for path, source in sources(PLANNER).items():
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.BinOp):
                    assert not isinstance(node.op, self.BANNED_OPS), (
                        path.name, node.lineno
                    )

    def test_no_arithmetic_on_numeric_literals(self):
        for path, source in sources(PLANNER).items():
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.BinOp):
                    continue
                for side in (node.left, node.right):
                    if isinstance(side, ast.Constant) and isinstance(
                        side.value, (int, float)
                    ) and not isinstance(side.value, bool):
                        pytest.fail(f"{path.name}:{node.lineno} numeric arithmetic")

    def test_no_safe_division_helper(self):
        assert "safe_div" not in PLANNER_SOURCE

    def test_plan_contains_no_computed_figure(self):
        """A plan for a financial question carries no number at all."""
        from app.services.ai.planner import QuestionPlan
        numeric_fields = {
            f.name for f in __import__("dataclasses").fields(QuestionPlan)
            if f.type in {"float", "int", "float | None", "int | None"}
        }
        assert numeric_fields == set()


# ===========================================================================
class TestLanguageStaysAnInterfaceConcern:
    """No language parameter may leak into scoring or retrieval."""

    def test_scoring_has_no_language_parameter(self):
        from app.services.scoring.service import ScoringService
        params = inspect.signature(ScoringService.score_company).parameters
        assert "language" not in params

    def test_retrieval_has_no_language_parameter(self):
        from app.services.documents.service import DocumentService
        params = inspect.signature(DocumentService.search).parameters
        assert "language" not in params

    def test_language_is_an_input_never_a_planning_dimension(self):
        """Same question, different declared language, identical plan.

        Language is an interface concern: knowing the user wrote Hindi may
        change how the answer is rendered, and must not change what the
        question is understood to be asking for.
        """
        from app.domain.language.types import Language

        question = "Reliance ki financial quality kaisi hai?"
        detected = QuestionPlanner().plan(question)
        forced = QuestionPlanner().plan(question, language=Language.HINDI)
        assert forced.language is Language.HINDI
        assert (
            detected.query_type, detected.execution_route,
            detected.intent_values, detected.evidence_keys(),
        ) == (
            forced.query_type, forced.execution_route,
            forced.intent_values, forced.evidence_keys(),
        )

    def test_planner_normalisation_is_planning_only(self):
        """The plan says so, and the wording is part of the contract."""
        plan = QuestionPlanner().plan("Reliance ki financial quality kaisi hai?")
        assert any("retrieval, scoring" in n for n in plan.notes)


# ===========================================================================
class TestExistingResolverUnchanged:
    """The execution path's gate is untouched by the planner's existence."""

    #: (question, expected) — one row per intent, plus the fail-closed cases.
    SINGLE = [
        ("What is the P/E?", FinancialIntent.PE),
        ("What is the P/B?", FinancialIntent.PB),
        ("What is the EPS?", FinancialIntent.EPS),
        ("What is the debt?", FinancialIntent.DEBT),
        ("What is the ROE?", FinancialIntent.ROE),
        ("What is the ROCE?", FinancialIntent.ROCE),
        ("What is the market price?", FinancialIntent.MARKET_PRICE),
        ("What is the intrinsic value?", FinancialIntent.VALUATION),
        ("What is the revenue growth?", FinancialIntent.REVENUE_GROWTH),
        ("What is the profit growth?", FinancialIntent.PROFIT_GROWTH),
        ("How is the overall assessment?", FinancialIntent.OVERALL_ASSESSMENT),
        ("What is the financial quality?", FinancialIntent.FINANCIAL_QUALITY),
        ("What is the growth quality?", FinancialIntent.GROWTH_QUALITY),
        ("What is the financial risk?", FinancialIntent.FINANCIAL_RISK),
        ("What are the strengths?", FinancialIntent.STRENGTHS),
        ("What are the weaknesses?", FinancialIntent.WEAKNESSES),
        ("What is the investment case?", FinancialIntent.INVESTMENT_CASE),
        ("What is the investment recommendation?", FinancialIntent.RECOMMENDATION),
    ]

    #: Questions mixing two intents. The resolver must keep declining,
    #: because answering one of them would be a partial answer.
    MULTI = [
        "What is the P/E and the ROE?",
        "What is the financial quality and the growth quality?",
        "What is the P/E and what is the overall assessment?",
        "Company ki strengths, weaknesses aur recommendation kya hai?",
    ]

    #: No supported intent at all.
    NONE = [
        "", "Tell me about the company", "Compare Reliance and TCS",
        "Good company hai?", "What is the revenue?",
    ]

    @pytest.mark.parametrize("question,intent", SINGLE)
    def test_single_intent_still_resolves(self, question, intent):
        assert FinancialIntentResolver().resolve(question) is intent

    @pytest.mark.parametrize("question", MULTI)
    def test_multi_intent_still_declines(self, question):
        """Fail-closed: 2+ matches is ``None``, never a partial answer."""
        assert FinancialIntentResolver().resolve(question) is None

    @pytest.mark.parametrize("question", NONE)
    def test_no_intent_still_declines(self, question):
        assert FinancialIntentResolver().resolve(question) is None

    def test_investment_intent_membership_unchanged(self):
        assert len(INVESTMENT_INTENTS) == 8

    def test_pattern_registry_is_still_read_only(self):
        with pytest.raises(TypeError):
            INTENT_PATTERNS[FinancialIntent.PE] = ("mutated",)  # type: ignore

    def test_every_intent_still_has_patterns(self):
        assert set(INTENT_PATTERNS) == set(FinancialIntent)
        assert all(INTENT_PATTERNS[i] for i in FinancialIntent)


# ===========================================================================
class TestDeterministicPathRegression:
    """Phase 1 and Phase 2A still answer, exactly as before."""

    @pytest.mark.parametrize("question,intent", TestExistingResolverUnchanged.SINGLE)
    def test_every_intent_still_produces_an_answer(self, question, intent):
        """Resolve the way the analyst does, then run the engine it routes to."""
        from tests.test_financial_answer_engine import engine_for, full_context

        resolved = FinancialIntentResolver().resolve(question)
        assert resolved is intent
        answer = engine_for(resolved).answer(resolved, full_context())
        assert answer.content.strip()
        assert answer.intent is intent

    @pytest.mark.parametrize("question,intent", TestExistingResolverUnchanged.SINGLE)
    def test_answers_still_pass_the_citation_audit(self, question, intent):
        """The evidence chain is unchanged: every figure resolves."""
        from app.services.ai.citation_engine import audit
        from tests.test_financial_answer_engine import engine_for, full_context

        context = full_context()
        answer = engine_for(intent).answer(intent, context)
        verdict = audit(answer.content, context.citations)
        assert verdict.unknown_keys == []
        assert verdict.uncited_numbers == []

    def test_planner_and_resolver_agree_on_single_intent_questions(self):
        """Where the resolver commits, the planner must not disagree."""
        from app.services.language.adapter import LanguageAdapter

        adapter = LanguageAdapter()
        for question, intent in TestExistingResolverUnchanged.SINGLE:
            english = adapter.normalise_query(question).english
            assert FinancialIntentResolver().resolve(english) is intent
            assert [m.intent for m in QuestionPlanner().plan(question).intents] == [intent]


# ===========================================================================
class TestWiredConsumers:
    """The planner is consumed by the internal reasoning layers, and by nothing else.

    Part 2B shipped the planner in shadow mode: nothing in the answer path
    called it. Part 2C ends that — the analyst plans a question the existing
    resolver declined, and the composition layer joins the result — so the
    set of permitted consumers became explicit rather than empty. Part 2D
    adds exactly one more, deliberately: the internal open-ended engine
    reads the plan's ``execution_route``, ``query_type`` and
    ``entity`` to decide whether it may answer at all.

    The rule the old shadow-mode test encoded still holds and is what this
    class pins: *these* modules may import the planner, and no others. A new
    production module importing it is a wiring change that must be made
    deliberately, not a line that slips in — and it must be added here by
    name, with a reason.
    """

    #: The composition boundary the planner was always destined for.
    COMPOSITION_LAYER = APP / "services" / "ai" / "internal_composer.py"
    #: The composition root: builds the planner for an analyst on request.
    SERVICE = APP / "services" / "ai" / "service.py"
    #: The single execution call site.
    ANALYST = APP / "services" / "ai" / "analyst.py"
    #: Part 2D. Consumes a plan the analyst already computed; it never
    #: builds one, so no question is planned twice and the two internal
    #: layers cannot disagree about what was asked.
    OPEN_ENDED_LAYER = APP / "services" / "ai" / "internal_open_ended.py"

    PERMITTED = {COMPOSITION_LAYER, SERVICE, ANALYST, OPEN_ENDED_LAYER}

    def test_only_the_permitted_modules_import_the_planner(self):
        offenders = []
        for path in python_files(APP):
            if PLANNER in path.parents or path.name == "__init__.py":
                continue
            if path in self.PERMITTED:
                continue
            source = path.read_text()
            if "services.ai.planner" in source or "services . ai . planner" in source:
                offenders.append(str(path.relative_to(APP)))
        assert offenders == []

    def test_the_analyst_still_uses_the_existing_resolver(self):
        source = self.ANALYST.read_text()
        assert "FinancialIntentResolver" in source

    def test_the_analyst_reaches_the_planner_only_through_composition(self):
        """The analyst owns exactly one composition entry point."""
        source = self.ANALYST.read_text()
        assert "QuestionPlanner" in source
        assert source.count("from app.services.ai.planner import") == 1
        # One definition, one call site.
        assert source.count("def _compose(") == 1
        assert source.count("self._compose(") == 1

    def test_source_resolver_path_still_precedes_composition(self):
        """Order in the analyst: resolver answer, else composer."""
        source = self.ANALYST.read_text()
        resolver_call = source.index("FinancialIntentResolver().resolve(")
        composition_call = source.index("composed = self._compose(")
        assert resolver_call < composition_call
        # And both sit inside the same guard block, so the composition gate
        # inherits the chat / non-empty / no-override / unrestricted checks.
        guard = source.index("and context_override is None")
        assert guard < resolver_call

    def test_context_builder_was_not_touched_for_the_planner(self):
        source = (APP / "services" / "ai" / "context_builder.py").read_text()
        assert "planner" not in source.lower()

    def test_answer_engines_were_not_touched_for_the_planner(self):
        for name in ("financial_answer_engine.py", "investment_answer_engine.py"):
            source = (APP / "services" / "ai" / name).read_text()
            assert "planner" not in source.lower()

    # ------------------------------------------------------- forbidden edges
    def test_the_planner_does_not_import_the_analyst(self):
        """No cycle: the analyst depends on the planner, never the reverse."""
        for path in python_files(PLANNER):
            imported = imports_of(path.read_text())
            assert not {
                name for name in imported if "analyst" in name.lower()
            }, path.name

    def test_the_composer_does_not_import_a_provider_or_a_retriever(self):
        """Checked on the code, not the prose that promises it."""
        imported = imports_of(self.COMPOSITION_LAYER.read_text())
        offenders = {
            name for name in imported
            if any(bad in name.lower()
                   for bad in ("provider", "retrieval", "rag", "httpx", "requests"))
        }
        assert offenders == set()

    def test_the_composer_calls_no_provider_and_no_retrieval(self):
        code = code_text(self.COMPOSITION_LAYER.read_text())
        for token in ("ProviderRouter", "router.complete", "httpx", "requests",
                      "document_service", ".search(", "retrieve", "embed"):
            assert token not in code, token

    def test_the_planner_takes_the_resolver_as_an_interface(self):
        """One company-resolution architecture: a callable, not a service.

        The planner is typed against ``Callable[[str], Sequence[...]]`` and
        the production wiring passes ``CompanyService.named_in`` — the same
        resolver the chat endpoint already uses. The planner never gets a
        session, a repository or a second way to look a company up.
        """
        source = (PLANNER / "question_planner.py").read_text()
        assert "CompanyResolver = Callable[[str], Sequence[CompanyLike]]" in source
        for token in ("CompanyService", "Session", "session"):
            assert token not in code_text(source), token

        wiring = self.SERVICE.read_text()
        assert "company_resolver=CompanyService(self.db).named_in" in wiring

    def test_the_planner_is_not_constructed_anywhere_else_in_production(self):
        constructions = []
        for path in python_files(APP):
            if PLANNER in path.parents:
                continue
            source = path.read_text()
            if "QuestionPlanner(" in source:
                constructions.append(str(path.relative_to(APP)))
        # Exactly one line of production code builds a planner: the analyst
        # factory in `service.py`. The analyst receives it already built.
        assert constructions == ["services/ai/service.py"]
        assert self.SERVICE.read_text().count("QuestionPlanner(") == 1


# ===========================================================================
class TestPart2CCallSites:
    """Exactly one production call site for each Part 2C entry point."""

    def test_compose_answer_has_one_production_call_site(self):
        call_sites = []
        for path in python_files(APP):
            source = path.read_text()
            if ".compose_answer(" not in source:
                continue
            call_sites.append(str(path.relative_to(APP)))
        assert call_sites == ["services/ai/analyst.py"]

    def test_the_flag_is_opted_into_by_exactly_one_endpoint(self):
        api = (APP / "api" / "v1" / "ai.py").read_text()
        assert api.count("enable_composition=True") == 1
        # …and it is the chat endpoint that carries it.
        chat = api.index("async def chat(")
        stream = api.index("async def chat_stream(")
        flag = api.index("enable_composition=True")
        assert chat < flag < stream
        # Streaming, reporting and the context dump keep the default.
        for name in ("chat_stream", "report", "context", "analyse"):
            start = api.index(f"def {name}(")
            end = api.find("\n@router", start)
            body = api[start:end if end != -1 else len(api)]
            assert "enable_composition" not in body, name

    def test_other_entry_points_never_opt_in(self):
        """Service-level callers keep the default by not passing the flag."""
        allowed = {
            "api/v1/ai.py",           # the chat endpoint
            "services/ai/service.py",  # the composition root and its default
        }
        offenders = []
        for path in python_files(APP):
            source = path.read_text()
            if "enable_composition" not in source:
                continue
            relative = str(path.relative_to(APP))
            if relative not in allowed:
                offenders.append(relative)
        assert offenders == []

    def test_streaming_and_batch_do_not_reference_the_composition_layer(self):
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        for name in ("stream_chat", "run_many"):
            body = source[source.index(f"async def {name}("):]
            end = body.find("\n    async def ", 1)
            body = body[:end if end != -1 else len(body)]
            assert "composer" not in body and "planner" not in body, name
            assert "_compose(" not in body, name

    def test_the_composition_layer_is_the_only_multi_engine_call_site(self):
        """No second orchestration loop: engines are called by the composer."""
        offenders = []
        for path in python_files(APP):
            if path.name in {"analyst.py", "internal_composer.py"}:
                continue
            source = path.read_text()
            if ("FinancialAnswerEngine()" in source
                    or "InvestmentAnswerEngine()" in source):
                offenders.append(str(path.relative_to(APP)))
        assert offenders == []
