"""Production verification against a live deployment.

    python3 deploy/verify_deployment.py --url https://<app>.up.railway.app

Checks every module the brief names — frontend, backend, AI, authentication,
reports, portfolio — plus HTTPS and the things that only break in production:
security headers, redirect behaviour, credential leakage, and whether the
schema actually migrated.

Exit code is 0 only if every **required** check passes. Optional checks are
reported and do not fail the run, because a feature that needs a paid API key
should not block a deploy that is otherwise sound.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TIMEOUT = 45


class Headers(dict):
    """Case-insensitive header access.

    `dict(response.headers)` throws away HTTP's case-insensitivity: urllib
    hands back lowercase keys, so `headers.get("X-Frame-Options")` returns
    None even when the header is present. That made the verifier report all
    five security headers as absent on a service that was sending every one
    of them — a false alarm in the tool whose entire job is telling the truth
    about a deployment.
    """

    def __init__(self, raw) -> None:
        super().__init__({str(k).lower(): v for k, v in dict(raw).items()})

    def get(self, key, default=None):  # noqa: A003
        return super().get(str(key).lower(), default)

    def __contains__(self, key) -> bool:  # noqa: D105
        return super().__contains__(str(key).lower())


@dataclass
class Check:
    name: str
    group: str
    ok: bool
    detail: str = ""
    ms: float = 0.0
    required: bool = True


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        mark = "PASS" if check.ok else ("FAIL" if check.required else "warn")
        colour = "\033[1;32m" if check.ok else ("\033[1;31m" if check.required else "\033[1;33m")
        print(f"  {colour}{mark:4}\033[0m {check.name:44} {check.ms:6.0f}ms  {check.detail[:60]}")
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.required]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.required]


def request(url: str, *, method: str = "GET", body: dict | None = None,
            headers: dict | None = None) -> tuple[int, dict | str, dict, float]:
    """Return (status, parsed body, headers, elapsed ms). Never raises."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            try:
                return response.status, json.loads(raw), Headers(response.headers), elapsed
            except json.JSONDecodeError:
                return response.status, raw.decode("utf8", "ignore"), Headers(response.headers), elapsed
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        raw = exc.read()
        try:
            return exc.code, json.loads(raw), Headers(exc.headers), elapsed
        except Exception:  # noqa: BLE001
            return exc.code, raw.decode("utf8", "ignore"), Headers(exc.headers), elapsed
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", Headers({}), (time.perf_counter() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="https://<app>.up.railway.app")
    parser.add_argument("--frontend-url", default=None)
    args = parser.parse_args()

    api = args.url.rstrip("/")
    web = (args.frontend_url or api).rstrip("/")
    report = Report()

    # ---------------------------------------------------------- HTTPS
    print("\n\033[1mHTTPS and transport\033[0m")
    report.add(Check("scheme is https", "https", api.startswith("https://"),
                     detail=api.split("://")[0]))

    if api.startswith("https://"):
        host = api.split("://")[1].split("/")[0]
        started = time.perf_counter()
        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(__import__("socket").create_connection((host, 443), timeout=20),
                                 server_hostname=host) as sock:
                cert = sock.getpeercert()
                issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "?")
                report.add(Check("TLS certificate valid", "https", True,
                                 f"issuer={issuer}, expires {cert.get('notAfter','?')}",
                                 (time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            report.add(Check("TLS certificate valid", "https", False, str(exc)[:60]))

        # http must not serve the app in the clear.
        code, _, headers, ms = request(api.replace("https://", "http://") + "/health")
        redirected = code in (301, 302, 307, 308) or code == 0 or code == 200
        report.add(Check("plain http does not serve content", "https",
                         code != 200 or "https" in str(headers.get("Location", "")),
                         f"HTTP {code}", ms, required=False))

    # ---------------------------------------------------------- backend
    print("\n\033[1mBackend\033[0m")
    code, body, headers, ms = request(f"{api}/health")
    report.add(Check("/health", "backend", code == 200 and isinstance(body, dict)
                     and body.get("status") == "ok",
                     f"HTTP {code}", ms))
    if isinstance(body, dict):
        report.add(Check("environment is production", "backend",
                         body.get("environment") == "production",
                         f"environment={body.get('environment')}", required=False))
        report.add(Check("authentication is enabled", "backend",
                         body.get("auth_enabled") is True,
                         f"auth_enabled={body.get('auth_enabled')}", required=False))

    code, body, _, ms = request(f"{api}/health/ready")
    ready = code == 200 and isinstance(body, dict) and body.get("ready") is True
    detail = ""
    if isinstance(body, dict):
        bad = [c["name"] for c in body.get("checks", []) if not c.get("ok")]
        detail = f"failing: {', '.join(bad)}" if bad else "all checks ok"
    report.add(Check("/health/ready", "backend", ready, detail, ms))

    # A ready service proves the schema migrated — the readiness probe
    # compares declared tables against present ones.
    if isinstance(body, dict):
        schema = next((c for c in body.get("checks", []) if c.get("name") == "schema"), None)
        report.add(Check("database schema migrated", "backend",
                         bool(schema and schema.get("ok")),
                         (schema or {}).get("detail", "no schema check reported")))

    code, body, _, ms = request(f"{api}/metrics")
    report.add(Check("/metrics", "backend", code == 200, f"HTTP {code}", ms, required=False))

    code, body, _, ms = request(f"{api}/openapi.json")
    paths = len(body.get("paths", {})) if isinstance(body, dict) else 0
    report.add(Check("OpenAPI schema served", "backend", code == 200 and paths > 100,
                     f"{paths} paths", ms))

    # ---------------------------------------------------------- security
    print("\n\033[1mSecurity headers\033[0m")
    # Read fresh. An earlier version reused `headers` from whichever call ran
    # last — by then the OpenAPI fetch — and reported every security header as
    # absent when all five were present. A verification tool that cries wolf
    # is worse than none: the next real failure gets dismissed.
    _, _, sec_headers, _ = request(f"{api}/health")
    headers = sec_headers
    for header, expected in (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
    ):
        value = headers.get(header, "")
        report.add(Check(f"{header}: {expected}", "security",
                         value.lower() == expected.lower(), value or "absent"))
    # HSTS is deliberately withheld outside production: pinning a developer's
    # browser to https for a host that serves plain http is very hard to undo.
    report.add(Check("Strict-Transport-Security", "security",
                     "Strict-Transport-Security" in headers,
                     headers.get("Strict-Transport-Security",
                                 "absent (expected outside production)"),
                     required=api.startswith("https://")))
    report.add(Check("Content-Security-Policy", "security",
                     "Content-Security-Policy" in headers,
                     headers.get("Content-Security-Policy", "absent")[:40]))

    # ---------------------------------------------------------- data
    print("\n\033[1mData and analysis modules\033[0m")
    code, body, _, ms = request(f"{api}/api/v1/companies?page_size=5")
    companies = body.get("results", []) if isinstance(body, dict) else []
    guarded = code in (401, 403)
    report.add(Check("company universe", "data", bool(companies) or guarded,
                     "guarded (401)" if guarded else
                     f"{body.get('total', 0) if isinstance(body, dict) else 0} companies", ms))

    ticker = companies[0]["ticker"] if companies else "RELIANCE"
    for label, path in (
        ("financials (M2)", f"/api/v1/company/{ticker}/financials"),
        ("ratios (M2)", f"/api/v1/company/{ticker}/ratios"),
        ("forecast (M3)", f"/api/v1/company/{ticker}/forecast?horizon=5"),
        ("valuation (M4)", f"/api/v1/company/{ticker}/valuation"),
        ("scoring (M5)", f"/api/v1/company/{ticker}/scoring"),
    ):
        code, _, _, ms = request(api + path)
        # 401/403 means mounted and guarded — the right answer once auth is on.
        report.add(Check(label, "data", code in (200, 401, 403),
                         f"HTTP {code} [{ticker}]", ms))

    # ---------------------------------------------------------- AI
    print("\n\033[1mAI layer\033[0m")
    code, body, _, ms = request(f"{api}/api/v1/ai/providers")
    configured = []
    if isinstance(body, dict):
        configured = [p["name"] for p in body.get("providers", []) if p.get("configured")]
    report.add(Check("provider registry", "ai", code in (200, 401, 403),
                     "guarded (401)" if code in (401, 403)
                     else f"configured: {', '.join(configured) or 'none'}", ms))

    if isinstance(body, dict):
        leaks = [p["endpoint"] for p in body.get("providers", []) if "key=" in p.get("endpoint", "")]
        report.add(Check("no API key in published endpoints", "ai", not leaks,
                         f"{len(leaks)} leak(s)" if leaks else "clean"))

    code, body, _, ms = request(f"{api}/api/v1/ai/health")
    if code == 200 and isinstance(body, dict):
        chain = " -> ".join(body.get("chain", []))
        serving = body.get("serving")
        report.add(Check("provider chain resolves", "ai", bool(body.get("chain")),
                         chain, ms))
        report.add(Check("a provider is serving", "ai", serving is not None,
                         f"serving={serving}", required=True))
        live = [p for p in body.get("providers", []) if p.get("status") == "ok"]
        report.add(Check("a LIVE model is reachable", "ai", bool(live),
                         "degraded to offline provider" if not live
                         else f"{live[0]['provider']} responding",
                         required=False))
        for provider in body.get("providers", []):
            if provider.get("status") not in ("ok", "ready"):
                report.add(Check(f"  {provider['provider']}", "ai", False,
                                 f"{provider['status']}: {provider['detail']}",
                                 required=False))
    elif code in (401, 403):
        # Authenticated-only in production. Reaching it needs a session, so
        # run this verifier again with a token to exercise the live probe.
        report.add(Check("provider health probe", "ai", True,
                         "guarded (401) — re-run with a session to probe live", ms))
    else:
        report.add(Check("provider health probe", "ai", False, f"HTTP {code}", ms))

    code, body, _, ms = request(f"{api}/api/v1/ai/capabilities")
    caps = len(body.get("capabilities", [])) if isinstance(body, dict) else 0
    report.add(Check("analyst capabilities", "ai",
                     caps >= 15 or code in (401, 403),
                     "guarded (401)" if code in (401, 403) else f"{caps} capabilities", ms))

    # ---------------------------------------------------------- auth
    print("\n\033[1mAuthentication\033[0m")
    code, body, _, ms = request(f"{api}/api/v1/auth/config")
    report.add(Check("auth config", "auth", code == 200, f"HTTP {code}", ms))
    if isinstance(body, dict):
        report.add(Check("native auth active", "auth", body.get("native_auth") is True,
                         f"native_auth={body.get('native_auth')}", required=False))

    code, _, headers, ms = request(f"{api}/api/v1/admin/members")
    report.add(Check("protected route rejects anonymous", "auth", code in (401, 403),
                     f"HTTP {code}", ms))

    code, _, _, ms = request(f"{api}/api/v1/platform/tenants")
    report.add(Check("operator console not publicly reachable", "auth",
                     code in (401, 403, 404), f"HTTP {code}", ms))

    code, body, _, ms = request(f"{api}/api/v1/auth/login", method="POST",
                                body={"email": "nobody@example.com", "password": "wrong-password"})
    report.add(Check("login rejects bad credentials", "auth", code in (401, 422, 429),
                     f"HTTP {code}", ms))

    # ---------------------------------------------------------- reports & portfolio
    print("\n\033[1mReports and portfolio\033[0m")
    # With NATIVE_AUTH on these require a session, so 401 is the *correct*
    # production answer and proves the module is mounted and guarded. What
    # would be wrong is 404 (not registered) or 500 (registered and broken).
    for label, path in (
        ("report capabilities (M9)", "/api/v1/reports/capabilities"),
        ("portfolio capabilities (M8)", "/api/v1/portfolios/capabilities"),
        ("document capabilities (M7)", "/api/v1/documents/capabilities"),
    ):
        code, _, _, ms = request(api + path)
        healthy = code == 200 or code in (401, 403)
        report.add(Check(label, "modules", healthy,
                         f"HTTP {code}" + (" (guarded)" if code in (401, 403) else ""), ms))

    # ---------------------------------------------------------- frontend
    print("\n\033[1mFrontend\033[0m")
    code, body, headers, ms = request(web)
    served = code == 200 and isinstance(body, str) and "<html" in body.lower()
    report.add(Check("frontend serves HTML", "frontend", served,
                     f"HTTP {code}, {len(body) if isinstance(body, str) else 0} bytes", ms,
                     required=False))

    # ---------------------------------------------------------- summary
    print("\n" + "=" * 78)
    total = len(report.checks)
    passed = sum(1 for c in report.checks if c.ok)
    print(f"  {passed}/{total} checks passed")
    if report.warned:
        print(f"  {len(report.warned)} warning(s) — non-blocking:")
        for check in report.warned:
            print(f"      {check.name}: {check.detail}")
    if report.failed:
        print(f"\n  \033[1;31m{len(report.failed)} REQUIRED CHECK(S) FAILED\033[0m")
        for check in report.failed:
            print(f"      {check.name}: {check.detail}")
        print("=" * 78)
        return 1
    print("\n  \033[1;32mAll required checks passed — deployment verified\033[0m")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
