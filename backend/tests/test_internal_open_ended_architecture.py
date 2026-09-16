"""Part 2D — architectural and security invariants for the internal open-ended engine.

`test_internal_open_ended.py` proves the engine answers the questions it
should and refuses the ones it should not. This module proves it does not
do the *wrong* thing, which is harder to notice: an internal reasoner that
quietly started calling a provider, or that grew its own citation model,
would still pass every behavioural test while ceasing to be the thing the
architecture says it is.

The rules are enforced by parsing the module's own source rather than by
inspecting one run, because each rule is about what the code is *permitted*
to depend on. Where behaviour matters more than structure — whether
importing the engine drags a provider into the process, whether answering
a question opens a socket — the check runs in a subprocess, so it cannot be
satisfied or defeated by whatever else this test session has already
loaded.

Two properties here are specific to Part 2D and did not exist before it.
The engine *computes*, so the arithmetic has to be a closed set rather than
an expression language: there is no ``eval``, no ``exec``, no shell and no
callable taken from data. And it *publishes* derived figures, so those
figures have to enter the one existing citation architecture rather than a
parallel one the audit cannot see.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.ai.context_builder import GroundedContext
from app.services.ai.internal_open_ended import (
    InternalOpenEndedEngine, OpenEndedCapability, OperationKind,
)
from app.services.ai.planner import QuestionPlanner

APP = Path(__file__).resolve().parent.parent / "app"
MODULE = APP / "services" / "ai" / "internal_open_ended.py"
SOURCE = MODULE.read_text()


def imports_of(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def code_text(source: str) -> str:
    """The source with every docstring removed.

    Token bans below are about what the code *does*, and a module docstring
    that says "there is no eval here" would otherwise trip the very check
    that enforces it. Stripping docstrings via the AST rather than by regex
    keeps comments and prose out of the wrong test.
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


#: The engine's executable surface — docstrings excluded.
CODE = code_text(SOURCE)
IMPORTED = imports_of(SOURCE)


# ===========================================================================
class TestNoExternalProviders:
    """18–21. The internal path has no LLM in it, anywhere."""

    FORBIDDEN = (
        "openai", "anthropic", "google", "google.generativeai", "genai",
        "groq", "mistralai", "cohere", "replicate", "openrouter",
        "httpx", "requests", "aiohttp", "urllib", "socket", "websockets",
        "app.services.ai.providers",
        "app.services.language.translators",
    )

    def test_the_module_imports_no_provider_or_client(self):
        offenders = {
            name for name in IMPORTED
            if any(name == bad or name.startswith(f"{bad}.")
                   for bad in self.FORBIDDEN)
        }
        assert offenders == set()

    @pytest.mark.parametrize("token", [
        "providerrouter", "openai", "openrouter", "gemini", "generativeai",
        "anthropic", "claude", "llmtranslator", "chatcompletion", "completion(",
    ])
    def test_the_code_never_names_a_provider(self, token):
        """Checked on the code, not on the prose that promises it."""
        assert token not in CODE.lower()

    def test_it_does_not_instantiate_a_provider_router(self):
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None,
                )
                assert name != "ProviderRouter"

    def test_importing_the_engine_loads_no_llm_provider_module(self):
        """A subprocess, so a prior import elsewhere cannot mask the answer.

        Scoped to the *LLM* provider package. ``app.data.providers`` — the
        finnhub / yahoo / fmp market-data clients — is loaded transitively
        by ``context_builder``, which this module imports for
        ``GroundedContext``; Part 2C's composer imports the same module and
        pulls in the same chain. What must not load is
        ``app.services.ai.providers``, the layer that would put an LLM in
        the internal path.
        """
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import app.services.ai.internal_open_ended\n"
            "loaded = sorted(m for m in sys.modules\n"
            "                if 'services.ai.providers' in m\n"
            "                or 'openai' in m or 'anthropic' in m\n"
            "                or 'google.generativeai' in m\n"
            "                or 'openrouter' in m)\n"
            "print(loaded)\n"
            "assert not loaded, loaded\n"
        ) % str(APP.parent)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_answering_a_question_loads_no_llm_provider(self):
        """Not just importing it — running it, in a clean process."""
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app.services.ai.context_builder import GroundedContext\n"
            "from app.services.ai.internal_open_ended import "
            "InternalOpenEndedEngine\n"
            "from app.services.ai.planner import QuestionPlanner\n"
            "ctx = GroundedContext(company_id='c', ticker='T', name='N',\n"
            "                      sector='FMCG')\n"
            "InternalOpenEndedEngine().answer(\n"
            "    QuestionPlanner().plan('What is the sector?'), ctx)\n"
            "loaded = sorted(m for m in sys.modules\n"
            "                if 'services.ai.providers' in m\n"
            "                or 'openai' in m or 'anthropic' in m\n"
            "                or 'google.generativeai' in m)\n"
            "assert not loaded, loaded\n"
            "print('clean')\n"
        ) % str(APP.parent)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "clean" in result.stdout

    def test_the_module_does_not_import_translators_directly(self):
        """Rendering belongs to the funnel, and this module has no part in it.

        The translators package is in the process because ``context_builder``
        reaches it; asserting on the direct import is what pins this
        module's own boundary.
        """
        assert not any(
            "translators" in name or "language.adapter" in name
            for name in IMPORTED
        )


# ===========================================================================
class TestNoNetworkDependency:
    """22. The successful internal path performs no I/O at all."""

    def test_no_networking_module_is_imported(self):
        offenders = {
            name for name in IMPORTED
            if any(name == bad or name.startswith(f"{bad}.") for bad in (
                "httpx", "requests", "aiohttp", "urllib", "urllib3", "socket",
                "ssl", "websockets", "http", "ftplib", "smtplib", "boto3",
            ))
        }
        assert offenders == set()

    def test_answering_opens_no_socket(self):
        """Socket creation is patched to fail, then the engine is run.

        Everything is imported before the patch: ``ssl`` subclasses
        ``socket.socket`` at import time, so patching first would break the
        interpreter rather than prove anything about this module.
        """
        code = (
            "import sys, socket; sys.path.insert(0, %r)\n"
            "from app.services.ai.context_builder import GroundedContext\n"
            "from app.domain.ai.types import Citation, EvidenceKind\n"
            "from app.services.ai.internal_open_ended import "
            "InternalOpenEndedEngine\n"
            "from app.services.ai.planner import QuestionPlanner\n"
            "def _boom(*a, **k):\n"
            "    raise AssertionError('the internal path opened a socket')\n"
            "socket.socket = _boom\n"
            "socket.create_connection = _boom\n"
            "socket.getaddrinfo = _boom\n"
            "ctx = GroundedContext(\n"
            "    company_id='c', ticker='T', name='N', sector='FMCG',\n"
            "    citations=[\n"
            "        Citation(key='revenue', label='Revenue',\n"
            "                 kind=EvidenceKind.STATEMENT, value=1000.0,\n"
            "                 unit='cr'),\n"
            "        Citation(key='pat', label='PAT',\n"
            "                 kind=EvidenceKind.STATEMENT, value=250.0,\n"
            "                 unit='cr'),\n"
            "    ])\n"
            "engine = InternalOpenEndedEngine()\n"
            "for q in ('What is the sector?', 'Compare revenue and pat',\n"
            "          'What is the order book?'):\n"
            "    engine.answer(QuestionPlanner().plan(q), ctx)\n"
            "print('no socket')\n"
        ) % str(APP.parent)
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no socket" in result.stdout

    def test_no_database_or_service_is_imported(self):
        """The engine reads a context it is handed; it fetches nothing."""
        offenders = {
            name for name in IMPORTED
            if any(bad in name for bad in (
                "sqlalchemy", "analysis_service", "scoring", "valuation",
                "forecast", "documents", "company_service", "models",
                "session",
            ))
        }
        assert offenders == set()

    def test_it_does_not_rebuild_the_context(self):
        """No duplicate ContextBuilder execution, and no re-querying."""
        assert "ContextBuilder" not in CODE
        assert "ContextBuilder(" not in CODE
        assert "SessionLocal" not in CODE


# ===========================================================================
class TestNoArbitraryCodeExecution:
    """23–25. Bounded reasoning means no interpreter."""

    @pytest.mark.parametrize("call", ["eval", "exec", "compile", "globals",
                                      "locals", "vars", "getattr", "setattr",
                                      "__import__"])
    def test_no_dynamic_execution_builtin(self, call):
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != call, call

    @pytest.mark.parametrize("module", [
        "subprocess", "os", "os.system", "pty", "shlex", "ctypes",
        "importlib", "pickle", "marshal",
    ])
    def test_no_shell_or_dynamic_import_module(self, module):
        assert not any(
            name == module or name.startswith(f"{module}.")
            for name in IMPORTED
        ), module

    def test_no_shell_token_appears_in_the_code(self):
        for token in ("subprocess", "os.system", "popen", "shell=True"):
            assert token not in CODE.lower(), token

    def test_operations_are_a_closed_enum_not_an_expression_language(self):
        """The arithmetic boundary is the type, not a convention."""
        assert {o.value for o in OperationKind} == {
            "lookup", "difference", "percentage_change", "ratio",
        }
        with pytest.raises(ValueError):
            OperationKind("__import__('os').system('ls')")

    def test_no_operation_carries_a_callable(self):
        """Data cannot smuggle behaviour into the executor."""
        for node in ast.walk(ast.parse(SOURCE)):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "InternalOperation":
                continue
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(
                    item.target, ast.Name,
                ):
                    annotation = ast.unparse(item.annotation)
                    for banned in ("Callable", "Any", "object", "lambda"):
                        assert banned not in annotation, (
                            node.name, item.target.id, annotation,
                        )

    def test_no_lambda_dispatches_an_operation(self):
        """A lambda is permitted as a sort key and for nothing else.

        The rule that matters is that no operation is executed by calling
        something taken from data. A ``key=lambda`` on a ``sort`` is an
        ordering predicate over the module's own tuples; a lambda stored in
        a table and called later would be an interpreter, which is the thing
        the closed ``OperationKind`` enum exists to prevent.
        """
        tree = ast.parse(SOURCE)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Lambda):
                continue
            parent = parents.get(node)
            assert isinstance(parent, ast.keyword) and parent.arg == "key", (
                "a lambda is used somewhere other than a sort key: "
                f"{ast.dump(node)}"
            )

    def test_no_callable_is_stored_in_a_dispatch_table(self):
        """Operations name a kind; the module owns the arithmetic."""
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
                for element in ast.walk(node):
                    if isinstance(element, ast.Lambda):
                        raise AssertionError(
                            "a callable is stored in data and would be "
                            "dispatched at runtime",
                        )


# ===========================================================================
class TestNoDuplicateArchitectures:
    """One citation model, one scoring model, one language model."""

    def test_citations_come_from_the_existing_type(self):
        assert "from app.domain.ai.types import" in SOURCE
        assert "class Citation" not in CODE
        assert "@dataclass" in SOURCE  # contracts, not a second evidence model

    def test_it_does_not_define_its_own_evidence_kind(self):
        assert "class EvidenceKind" not in CODE
        # Every kind it names is an existing member.
        from app.domain.ai.types import EvidenceKind
        for node in ast.walk(ast.parse(SOURCE)):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "EvidenceKind"):
                assert hasattr(EvidenceKind, node.attr), node.attr

    def test_it_does_not_reimplement_the_citation_audit(self):
        assert "def audit" not in CODE
        assert "def annotate" not in CODE
        assert "citation_engine" not in IMPORTED

    def test_it_does_not_duplicate_a_scoring_or_valuation_engine(self):
        for token in ("ScoringService", "ValuationService", "ForecastService",
                      "RatioService", "ScoreResult", "overall_score"):
            assert token not in CODE, token

    def test_it_does_not_duplicate_a_financial_answer_engine(self):
        """It reads citations; it does not re-derive a figure an engine owns."""
        for token in ("FinancialAnswerEngine", "InvestmentAnswerEngine",
                      "_build_pe", "_build_roe", "_build_debt"):
            assert token not in CODE, token

    def test_no_financial_formula_is_reimplemented(self):
        """WACC, DCF, growth and margins are computed elsewhere, once."""
        for token in ("wacc", "dcf", "terminal value", "cagr"):
            assert token not in CODE.lower(), token

    def test_it_does_not_create_a_second_language_path(self):
        for token in ("LanguageAdapter", "translate", "translators",
                      "internal_renderer", "glossary"):
            assert token not in CODE.lower(), token

    def test_it_does_not_resolve_a_company(self):
        """Identity belongs to the analyst's existing gate."""
        for token in ("CompanyService", "named_in", "company_resolver"):
            assert token not in CODE, token

    def test_arithmetic_reuses_the_platforms_own_division(self):
        """`safe_div` is where the undefined-ratio rule is defined, once."""
        assert "from app.domain.calc import safe_div" in SOURCE
        # The one subtraction this layer performs is explicit and total;
        # every division goes through the shared primitive.
        assert CODE.count("safe_div(") >= 1


# ===========================================================================
class TestPlannerConsumersRemainExplicit:
    """The engine consumes a plan; it never builds one."""

    def test_it_never_constructs_a_planner(self):
        assert "QuestionPlanner(" not in CODE

    def test_it_never_calls_plan(self):
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "plan"

    def test_it_reads_the_plan_as_data_only(self):
        """A one-way dependency: the planner does not know this module exists."""
        planner_dir = APP / "services" / "ai" / "planner"
        for path in sorted(planner_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            assert "internal_open_ended" not in path.read_text(), path.name

    def test_the_engine_is_listed_as_a_permitted_planner_consumer(self):
        """The allowlist is updated by name, not by accident."""
        from tests.test_question_planner_architecture import TestWiredConsumers

        assert MODULE in TestWiredConsumers.PERMITTED


# ===========================================================================
class TestProviderFallbackRemainsAvailable:
    """Part 2D adds a route; it removes none. Part 2E is a separate task."""

    def test_every_provider_module_still_exists(self):
        providers = APP / "services" / "ai" / "providers"
        for name in ("router.py", "base.py", "gemini.py", "openai.py",
                     "openrouter.py", "claude.py", "mock.py"):
            assert (providers / name).is_file(), name

    def test_the_router_is_still_imported_by_the_analyst(self):
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        assert "from app.services.ai.providers.router import ProviderRouter" in source

    def test_the_analyst_still_retrieves_and_calls_a_provider(self):
        """The fallback path is intact, not stubbed out."""
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        assert "self._retrieve(" in source
        assert "self.router.complete(" in source

    def test_the_internal_route_sits_after_the_existing_routes(self):
        """Order in the analyst: resolver, composer, then Part 2D."""
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        resolver_call = source.index("FinancialIntentResolver().resolve(")
        compose_call = source.index("composed = self._compose(")
        internal_call = source.index("internal = self._open_ended(")
        assert resolver_call < compose_call < internal_call
        # All three sit inside the same guard block, so the internal layers
        # inherit the chat / non-empty / no-override / unrestricted checks.
        guard = source.index("and context_override is None")
        assert guard < resolver_call

    def test_the_retrieval_and_provider_path_was_not_edited_away(self):
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        assert "retrieved = self._retrieve(" in source
        assert "directive.scope.is_restricted" in source


# ===========================================================================
class TestVerificationFunnelRemainsShared:
    """There is one funnel, and Part 2D goes through it."""

    def test_the_engine_does_not_verify_or_guardrail_anything_itself(self):
        for token in ("audit(", "check(", "enforce(", "annotate(",
                      "GuardrailReport", "CitationAudit"):
            assert token not in CODE, token

    def test_the_analyst_has_exactly_one_verification_funnel(self):
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        assert source.count("def _verify_and_record(") == 1
        assert source.count("citation_audit = audit(") == 1

    def test_the_internal_answer_reaches_that_funnel(self):
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        internal_block = source[source.index("internal = self._open_ended("):]
        assert "await self._deterministic(" in internal_block[:600]

    def test_the_deterministic_funnel_is_shared_by_all_three_shapes(self):
        """One funnel, widened by a parameter rather than forked."""
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        assert source.count("async def _deterministic(") == 1
        signature = source[
            source.index("async def _deterministic("):
            source.index("async def _deterministic(") + 500
        ]
        assert "DeterministicAnswer" in signature
        assert "ComposedAnswer" in signature
        assert "InternalAnswer" in signature

    def test_the_language_adapter_is_still_invoked_last(self):
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        funnel = source[source.index("async def _verify_and_record("):]
        assert funnel.index("citation_audit = audit(") < funnel.index(
            "LanguageAdapter()",
        )

    def test_guardrails_are_not_bypassed_for_an_internal_answer(self):
        """The funnel is not conditional on where the answer came from."""
        source = (APP / "services" / "ai" / "analyst.py").read_text()
        funnel = source[source.index("async def _verify_and_record("):]
        for call in ("audit(raw_content", "check(raw_content",
                     "enforce(raw_content", "annotate(content"):
            assert call in funnel, call


# ===========================================================================
class TestCapabilityTaxonomyIsClosed:
    """The capability set is a table, not something that grows at runtime."""

    def test_every_capability_has_an_executor_or_is_a_refusal(self):
        """No capability the module advertises and cannot deliver."""
        executed = {
            OpenEndedCapability.FACT_LOOKUP,
            OpenEndedCapability.COMPARISON,
            OpenEndedCapability.MULTI_STEP_ANALYSIS,
            OpenEndedCapability.EXPLANATION,
            OpenEndedCapability.CALCULATION,
        }
        refusals = {
            OpenEndedCapability.UNSUPPORTED_OPEN_ENDED,
            OpenEndedCapability.AMBIGUOUS,
        }
        assert executed | refusals == set(OpenEndedCapability)

    def test_the_calculation_capability_is_exercised_by_the_operations(self):
        """CALCULATION is a taxonomy member; the arithmetic is the operation
        set. A capability with no operation behind it would be decoration."""
        assert {
            OperationKind.DIFFERENCE, OperationKind.RATIO,
            OperationKind.PERCENTAGE_CHANGE,
        } <= set(OperationKind)

    def test_the_vocabulary_tables_are_module_literals(self):
        """Bounded, reviewable, and not fetched or generated."""
        from app.services.ai.internal_open_ended import (
            ATTRIBUTE_SPECS, EXPLANATION_SPECS, METRIC_SPECS,
        )
        for table in (METRIC_SPECS, ATTRIBUTE_SPECS, EXPLANATION_SPECS):
            assert isinstance(table, tuple)
            assert table  # not silently emptied

    def test_the_operation_cap_is_a_constant(self):
        from app.services.ai.internal_open_ended import MAX_OPERATIONS

        assert isinstance(MAX_OPERATIONS, int)
        assert 1 <= MAX_OPERATIONS <= 20


# ===========================================================================
class TestEngineIsStatelessAndReusable:
    """Built once per analyst, like the composer."""

    def test_the_engine_holds_no_state(self):
        engine = InternalOpenEndedEngine()
        assert not [
            name for name in vars(engine)
        ], "the engine must be stateless after construction"

    def test_the_same_question_yields_the_same_answer(self):
        """Determinism, checked by running it twice."""
        context = GroundedContext(
            company_id="c", ticker="T", name="Deterministic Ltd",
            sector="FMCG",
        )
        plan = QuestionPlanner().plan("What is the sector?")
        engine = InternalOpenEndedEngine()

        first = engine.answer(plan, context)
        second = engine.answer(plan, context)

        assert first == second
        assert first.content == second.content

    def test_two_engines_agree(self):
        context = GroundedContext(
            company_id="c", ticker="T", name="Deterministic Ltd",
            sector="FMCG",
        )
        plan = QuestionPlanner().plan("What is the sector?")
        assert (InternalOpenEndedEngine().answer(plan, context)
                == InternalOpenEndedEngine().answer(plan, context))
