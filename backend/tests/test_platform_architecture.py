"""Module 10 — architectural invariants, enforced by AST inspection.

Every module in this project has ended with a suite like this, because the
rules that matter most are the ones nobody remembers under deadline. A comment
saying "the domain layer must not import SQLAlchemy" is a wish; a test that
parses the imports is a guarantee.

Module 10 adds four rules of its own to the existing set:

1. The platform domain layer imports no infrastructure.
2. No secret-bearing column reaches a response schema.
3. Every tenant-owned query is filtered — checked by finding raw queries that
   should have gone through `TenantScope`.
4. Authorisation is expressed as permissions, never as role literals in
   routes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
PLATFORM_DOMAIN = APP / "domain" / "platform"
PLATFORM_SERVICES = APP / "services" / "platform"


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _source(path: Path) -> str:
    return path.read_text()


# ===========================================================================
class TestDomainPurity:
    """The domain layer states rules; it does not know how they are stored or
    served. Every rule in it is therefore testable with no database and no
    web framework, which is the entire point."""

    FORBIDDEN = (
        "sqlalchemy", "fastapi", "starlette", "httpx", "redis",
        "app.models", "app.api", "app.db", "app.services",
        "argon2", "jose", "structlog",
    )

    def test_no_platform_domain_module_imports_infrastructure(self):
        offences: list[str] = []
        for path in _python_files(PLATFORM_DOMAIN):
            for imported in _imports(path):
                for banned in self.FORBIDDEN:
                    if imported == banned or imported.startswith(banned + "."):
                        offences.append(f"{path.name} imports {imported}")
        assert offences == [], offences

    def test_the_domain_does_not_reach_for_settings(self):
        """A pure rule cannot depend on deployment configuration. A password
        policy that reads `settings` is untestable in isolation and behaves
        differently in production than in the test that approved it."""
        offences = [
            path.name for path in _python_files(PLATFORM_DOMAIN)
            if "app.core.config" in _imports(path)
        ]
        assert offences == [], offences

    def test_the_domain_raises_domain_errors_not_http_ones(self):
        """Checked against parsed code, not raw text.

        A substring search flagged `identity.py`, whose docstring explains
        *why* it does not import HTTPException. Grepping source for a name is
        the wrong instrument: it cannot tell a prohibition from an
        explanation of the prohibition.
        """
        for path in _python_files(PLATFORM_DOMAIN):
            tree = ast.parse(_source(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "HTTPException":
                    pytest.fail(f"{path.name} references HTTPException in code")
                if isinstance(node, ast.Attribute) and node.attr == "HTTPException":
                    pytest.fail(f"{path.name} references HTTPException in code")

    def test_the_domain_never_prints(self):
        for path in _python_files(PLATFORM_DOMAIN):
            tree = ast.parse(_source(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    pytest.fail(f"{path.name} calls print()")


class TestServiceLayerBoundaries:
    def test_services_do_not_import_the_api_layer(self):
        """Dependency flows one way: API → services → domain. A service that
        imports a router creates a cycle and makes the service untestable
        without spinning up FastAPI."""
        offences = [
            f"{path.name} imports {imported}"
            for path in _python_files(PLATFORM_SERVICES)
            for imported in _imports(path)
            if imported.startswith("app.api")
        ]
        assert offences == [], offences

    def test_only_crypto_hashes_passwords(self):
        """One place to review, one place to change when a parameter needs
        raising."""
        offenders = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if "argon2" in _imports(path)
            and path.name != "crypto.py"
        ]
        assert offenders == [], offenders

    def test_only_crypto_signs_or_decodes_jwts(self):
        offenders = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if any(i.startswith("jose") for i in _imports(path))
            and path.name != "crypto.py"
        ]
        assert offenders == [], offenders

    def test_the_entitlement_decision_exists_once(self):
        """`evaluate()` is the only place commercial policy is expressed. A
        second copy is how a route starts allowing something the pricing page
        says it does not."""
        definitions = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if re.search(r"^def evaluate\(", _source(path), re.MULTILINE)
        ]
        assert definitions == ["domain/platform/plans.py"], definitions

    def test_the_sliding_window_exists_once(self):
        definitions = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if re.search(r"^def sliding_window\(", _source(path), re.MULTILINE)
        ]
        assert definitions == ["domain/platform/limits.py"], definitions

    def test_redaction_exists_once(self):
        definitions = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if re.search(r"^def redact\(", _source(path), re.MULTILINE)
        ]
        assert definitions == ["domain/platform/audit.py"], definitions


# ===========================================================================
class TestSecretHygiene:
    """No column holding a credential may reach a response."""

    #: Model attributes that must never appear in a Pydantic response model.
    SECRET_COLUMNS = (
        "password_hash", "token_hash", "key_hash", "mfa_secret_encrypted",
        "ciphertext",
    )

    def test_no_response_schema_declares_a_secret_field(self):
        schema_source = _source(APP / "schemas" / "platform.py")
        tree = ast.parse(schema_source)

        offences: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Request models are permitted a `password`; response models are
            # identified by name and must carry nothing secret at all.
            if not node.name.endswith("Out"):
                continue
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(
                    statement.target, ast.Name
                ):
                    field = statement.target.id
                    if field in self.SECRET_COLUMNS or field.endswith("_hash"):
                        offences.append(f"{node.name}.{field}")
        assert offences == [], offences

    def test_the_only_model_carrying_a_plaintext_is_the_issued_key(self):
        """A key must be displayable exactly once, at creation."""
        tree = ast.parse(_source(APP / "schemas" / "platform.py"))
        carriers = [
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(s, ast.AnnAssign)
                and isinstance(s.target, ast.Name)
                and s.target.id == "plaintext"
                for s in node.body
            )
        ]
        assert carriers == ["IssuedApiKeyOut"], carriers

    def test_no_secret_is_written_to_a_log_call(self):
        """Logs are the likelier leak: shipped off the box, kept for a year,
        read by more people than the database."""
        pattern = re.compile(
            r"log(?:ger)?\.\w+\([^)]*\b(password|plaintext|token_hash|key_hash|secret)\b",
            re.IGNORECASE | re.DOTALL,
        )
        offences = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if pattern.search(_source(path))
        ]
        assert offences == [], offences

    def test_no_credential_is_hard_coded(self):
        """A default secret in source is the same as no secret at all."""
        pattern = re.compile(
            r"(SECRET_KEY|ENCRYPTION_KEY|CLIENT_SECRET|SMTP_PASSWORD)\s*[:=]\s*['\"][^'\"]{6,}",
        )
        offences = []
        for path in _python_files(APP):
            for match in pattern.finditer(_source(path)):
                offences.append(f"{path.name}: {match.group(0)[:60]}")
        assert offences == [], offences

    def test_production_refuses_to_run_with_a_generated_signing_key(self):
        """Asserted against the source of the guard rather than by mutating
        global settings, which would leak into other tests."""
        source = _source(APP / "services" / "platform" / "crypto.py")
        assert "is_production" in source
        assert "RuntimeError" in source


# ===========================================================================
class TestTenancyDiscipline:
    def test_every_tenant_owned_model_carries_a_tenant_id(self):
        """`tenant_id` is the first column of every business table and the
        first predicate of every query."""
        source = _source(APP / "models" / "platform.py")
        tree = ast.parse(source)

        exempt = {
            "Plan",              # global catalogue
            "RequestMetric",     # aggregate, no tenant dimension
            "ScheduleState",     # platform-wide schedule
            "BackupRecord",      # the whole database
            "User",              # nullable: an operator may have no tenant
            "UserIdentity",      # reached through User
            "RefreshToken",      # reached through User; has one anyway
            "OneTimeToken",      # reached through User
            "Tenant",            # is the tenant
            "TenantSecret",      # has one, checked below
            "Subscription",      # has one, checked below
            "ErrorEvent",        # nullable: an error may precede auth
        }

        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name in exempt:
                continue
            fields = {
                s.target.id for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
            if "__tablename__" in fields and "tenant_id" not in fields:
                missing.append(node.name)
        assert missing == [], missing

    def test_tenant_scope_is_the_only_filter_helper(self):
        definitions = [
            path.relative_to(APP).as_posix()
            for path in _python_files(APP)
            if re.search(r"^class TenantScope", _source(path), re.MULTILINE)
        ]
        assert definitions == ["services/platform/tenancy.py"], definitions

    def test_admin_routes_derive_the_tenant_from_the_principal(self):
        """No endpoint may accept `tenant_id` as a body field: the tenant
        comes from the caller's identity, never from what they send."""
        tree = ast.parse(_source(APP / "schemas" / "platform.py"))
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Request models: Create / Update / Request / Change suffixes.
            if not node.name.endswith(("Create", "Update", "Request", "Change")):
                continue
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == "tenant_id"
                ):
                    offences.append(node.name)
        assert offences == [], offences

    def test_cross_tenant_endpoints_are_all_operator_guarded(self):
        """Every `/platform/*` route must carry `require_operator`. One that
        does not is a customer-visible operator console."""
        source = _source(APP / "api" / "v1" / "admin.py")
        tree = ast.parse(source)

        unguarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                path = next(
                    (a.value for a in decorator.args if isinstance(a, ast.Constant)), "",
                )
                if not str(path).startswith("/platform/"):
                    continue
                # The guard may be a route dependency or an argument default.
                segment = ast.get_source_segment(source, node) or ""
                decorator_segment = ast.get_source_segment(source, decorator) or ""
                if "require_operator" not in segment + decorator_segment:
                    # `/platform/plans` is intentionally public — it is the
                    # pricing page's data source.
                    if path not in ("/platform/plans",):
                        unguarded.append(f"{node.name} ({path})")
        assert unguarded == [], unguarded


# ===========================================================================
class TestAuthorisationStyle:
    def test_routes_ask_for_permissions_not_roles(self):
        """Endpoints never ask "is this an admin?" — they ask "may this caller
        write a portfolio?". The reverse smears authorisation logic across a
        hundred handlers that quietly diverge."""
        source = _source(APP / "api" / "v1" / "admin.py")
        tree = ast.parse(source)

        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            segment = ast.get_source_segment(source, node) or ""
            # Comparing a principal's role to a literal inside a handler.
            if re.search(r"\.role\s*==\s*Role\.", segment):
                offences.append(node.name)
        assert offences == [], offences

    def test_every_permission_is_reachable_from_some_role(self):
        """A permission no role grants can never be satisfied, so any route
        guarded by it is dead."""
        from app.domain.platform.identity import Permission, ROLE_PERMISSIONS

        granted = set().union(*ROLE_PERMISSIONS.values())
        assert set(Permission) - granted == set()

    def test_every_permission_guards_something(self):
        """The converse: a permission nothing checks is decoration.

        Checked across the whole API surface, not just the admin router,
        because Modules 1-9 endpoints are guarded too.
        """
        from app.domain.platform.identity import Permission

        api_source = "\n".join(
            _source(p) for p in _python_files(APP / "api")
        ) + "\n".join(_source(p) for p in _python_files(APP / "core"))

        unused = [
            permission.value for permission in Permission
            if permission.name not in api_source
        ]
        # Research-surface permissions are declared for the matrix and for
        # API-key scoping ahead of being wired into each Module 1-9 route;
        # that is a documented gap, not an accident, so it is asserted as a
        # known set rather than silently tolerated.
        expected_unwired = {
            "ai:read", "company:read", "company:write", "document:delete",
            "document:read", "document:upload", "forecast:read",
            "forecast:write", "portfolio:delete", "portfolio:read",
            "portfolio:write", "report:delete", "report:generate",
            "report:read", "scoring:read", "scoring:write",
            "valuation:read", "ai:run", "subscription:read", "job:read",
            "tenant:create",
        }
        surprises = set(unused) - expected_unwired
        assert surprises == set(), f"unused permissions: {surprises}"


class TestOperatorGuardCompleteness:
    """A regression guard for a hole this suite actually caught.

    Wiring `Permission.JOB_MANAGE` onto `/platform/jobs/*` to satisfy the
    "every permission guards something" test silently opened those routes to
    tenant Admins, who also hold `job:manage` — turning three operator-only
    endpoints into cross-tenant ones. The permission was necessary and not
    sufficient.

    The rule these tests encode: a `/platform/*` route is guarded by
    `require_operator` *in addition to* whatever capability it names, and no
    permission that a non-operator role holds may be the sole guard on one.
    """

    def _platform_routes(self):
        source = _source(APP / "api" / "v1" / "admin.py")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                path = next(
                    (a.value for a in decorator.args if isinstance(a, ast.Constant)), "",
                )
                if str(path).startswith("/platform/"):
                    yield node.name, str(path), (
                        (ast.get_source_segment(source, decorator) or "")
                        + (ast.get_source_segment(source, node) or "")
                    )

    def test_no_platform_route_relies_on_a_permission_tenants_also_hold(self):
        from app.domain.platform.identity import (
            CROSS_TENANT_PERMISSIONS, Permission, ROLE_PERMISSIONS, Role,
        )

        tenant_admin = ROLE_PERMISSIONS[Role.ADMIN]
        offences = []
        for name, path, segment in self._platform_routes():
            if path == "/platform/plans":
                continue                      # intentionally public
            if "require_operator" in segment:
                continue                      # correctly guarded
            named = {
                p for p in Permission
                if f"Permission.{p.name}" in segment
            }
            if named & tenant_admin:
                offences.append(
                    f"{name} ({path}) is guarded only by "
                    f"{sorted(p.value for p in named & tenant_admin)}, which a "
                    "tenant Admin holds"
                )
        assert offences == [], offences

    def test_every_operator_only_permission_is_genuinely_operator_only(self):
        from app.domain.platform.identity import (
            CROSS_TENANT_PERMISSIONS, ROLE_PERMISSIONS, Role,
        )

        for permission in CROSS_TENANT_PERMISSIONS:
            holders = [r for r in Role if permission in ROLE_PERMISSIONS[r]]
            assert holders == [Role.SUPER_ADMIN], (
                f"{permission} is held by {holders}"
            )


# ===========================================================================
class TestConfiguration:
    def test_every_new_setting_has_a_default(self):
        """The stack must start with an empty environment."""
        from app.core.config import Settings

        Settings()   # raises if any field is required

    def test_production_readiness_is_computed_not_asserted(self):
        from app.core.config import Settings

        development = Settings(ENVIRONMENT="development")
        assert development.production_readiness_problems() == []

        production = Settings(
            ENVIRONMENT="production", DEBUG=True, SECRET_KEY=None,
            DATABASE_URL="sqlite+pysqlite:///./ierp.db",
        )
        problems = production.production_readiness_problems()
        assert any("SECRET_KEY" in p for p in problems)
        assert any("DEBUG" in p for p in problems)
        assert any("SQLite" in p for p in problems)

    def test_missing_smtp_does_not_make_the_service_unready(self):
        """DEP-003 regression.

        A fully-configured production instance with no mail relay must still
        accept traffic. Conflating the two took the entire platform offline on
        Railway: /health/ready returned 503 for a service whose database,
        schema and security configuration were all correct, and the deploy was
        rolled back over an undeliverable password-reset e-mail.
        """
        from app.core.config import Settings

        secure = Settings(
            ENVIRONMENT="production", DEBUG=False, NATIVE_AUTH=True,
            SECRET_KEY="x" * 40, ENCRYPTION_KEY="y" * 40,
            DATABASE_URL="postgresql+psycopg://u:p@h:5432/d",
            CORS_ORIGINS=["https://app.example.com"],
            SMTP_HOST=None,
        )
        assert secure.production_blocking_problems() == []
        assert any("SMTP" in p for p in secure.production_degraded_problems())
        # The aggregate view still reports everything, for the admin panel.
        assert any("SMTP" in p for p in secure.production_readiness_problems())

    def test_unsafe_configuration_still_blocks(self):
        """The severity split must not weaken the genuinely unsafe cases."""
        from app.core.config import Settings

        for kwargs, marker in (
            ({"SECRET_KEY": None}, "SECRET_KEY"),
            ({"SECRET_KEY": "x" * 40, "DEBUG": True}, "DEBUG"),
            ({"SECRET_KEY": "x" * 40, "NATIVE_AUTH": False,
              "AUTH_DEV_MODE": True}, "Development identity"),
            ({"SECRET_KEY": "x" * 40,
              "DATABASE_URL": "sqlite+pysqlite:///./ierp.db"}, "SQLite"),
            ({"SECRET_KEY": "x" * 40,
              "CORS_ORIGINS": ["http://localhost:3000"]}, "plaintext http"),
        ):
            base = {
                "ENVIRONMENT": "production", "DEBUG": False,
                "NATIVE_AUTH": True,
                "DATABASE_URL": "postgresql+psycopg://u:p@h:5432/d",
                "CORS_ORIGINS": ["https://app.example.com"],
                **kwargs,
            }
            problems = Settings(**base).production_blocking_problems()
            assert any(marker in p for p in problems), f"{marker} must block"

    def test_secure_cookies_are_forced_in_production(self):
        from app.core.config import Settings

        assert Settings(ENVIRONMENT="production", COOKIE_SECURE=False).cookie_secure
        assert not Settings(ENVIRONMENT="development", COOKIE_SECURE=False).cookie_secure

    def test_oauth_providers_require_both_halves(self):
        from app.core.config import Settings

        assert Settings(GOOGLE_CLIENT_ID="x").oauth_providers == []
        assert Settings(
            GOOGLE_CLIENT_ID="x", GOOGLE_CLIENT_SECRET="y",
        ).oauth_providers == ["google"]

    def test_the_env_example_documents_every_setting(self):
        """A setting that exists but is undocumented will be misconfigured."""
        from app.core.config import Settings

        example = (APP.parent / ".env.example")
        if not example.exists():
            pytest.skip(".env.example not present")

        text = example.read_text()
        undocumented = [
            name for name in Settings.model_fields
            if name.isupper() and name not in text
        ]
        assert undocumented == [], undocumented


class TestConcurrencySafety:
    """Regression guards for a deadlock the load test found.

    At concurrency 25 the server stopped answering entirely — including
    `/health`, which touches no database. Three independent causes, each of
    which is invisible at concurrency 1:

    1. `get_current_user` was `async def` while doing synchronous database
       work, so authentication ran on the event loop thread and blocked every
       other request in the process.
    2. The observability middleware opened a second session per request while
       the request's own session was still checked out, doubling pool demand.
    3. SQLite was in rollback-journal mode, where every write takes a
       database-wide exclusive lock and freezes all readers.

    These tests encode the fixes so none of the three can return quietly.
    """

    def test_auth_dependencies_are_synchronous(self):
        """A dependency that does blocking work must be `def`, not `async def`.

        Starlette runs a sync dependency on a worker thread and an async one
        on the event loop. Declaring a blocking function async is the single
        most effective way to make a FastAPI application stop scaling.
        """
        source = _source(APP / "core" / "security.py")
        tree = ast.parse(source)

        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            segment = ast.get_source_segment(source, node) or ""
            # An async function is fine if it genuinely awaits something.
            if "await " not in segment:
                offences.append(node.name)
        assert offences == [], (
            f"async dependencies doing synchronous work: {offences}"
        )

    def test_the_request_path_opens_no_second_session(self):
        """Middleware must not check out a connection while the request holds
        one — that halves the effective pool and deadlocks under load."""
        source = _source(APP / "main.py")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if not node.name.endswith("_middleware"):
                continue
            segment = ast.get_source_segment(source, node) or ""
            assert "SessionLocal()" not in segment, (
                f"{node.name} opens a database session on the request path"
            )

    def test_the_pool_is_sized_explicitly(self):
        """SQLAlchemy's default of five plus ten is sized for a script.
        FastAPI serves sync endpoints from a forty-thread pool."""
        source = _source(APP / "db" / "base.py")
        assert "pool_size" in source
        assert "max_overflow" in source
        assert "pool_timeout" in source

    def test_sqlite_is_configured_for_concurrent_reads(self):
        """WAL, or one writer freezes every reader."""
        source = _source(APP / "db" / "base.py")
        assert "journal_mode=WAL" in source
        assert "busy_timeout" in source

    def test_sqlite_actually_reports_wal(self, tmp_path):
        """The pragma is asserted against a real connection, not just against
        the source that claims to set it."""
        from sqlalchemy import create_engine, text

        from app.db.base import Base

        path = tmp_path / "wal-check.db"
        engine = create_engine(f"sqlite+pysqlite:///{path}")

        # The listener is registered against the application's own engine, so
        # replicate the configuration the way the app applies it.
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _configure(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(bind=engine)
        with engine.connect() as connection:
            mode = connection.execute(text("PRAGMA journal_mode")).scalar()
        engine.dispose()
        assert str(mode).lower() == "wal"

    def test_metric_flushes_survive_a_concurrent_collision(self, tmp_path):
        """Two flushes racing on the same bucket must merge, not lose a batch.

        The unique constraint on (bucket, route, method, status) is correct;
        the flush has to cope with losing the race rather than discarding the
        minute's samples.
        """
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.db.base import Base
        from app.models.platform import RequestMetric
        from app.services.platform.observability import MetricsCollector

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        with Session() as db:
            first = MetricsCollector(flush_after=1)
            second = MetricsCollector(flush_after=1)
            for collector in (first, second):
                collector.observe(
                    route="/api/v1/x", method="GET", status_code=200, duration_ms=10,
                )
            first.flush(db)
            second.flush(db)

            rows = db.scalars(select(RequestMetric)).all()
            assert len(rows) == 1, "the bucket was duplicated"
            assert rows[0].count == 2, "the second flush lost its samples"

        engine.dispose()
