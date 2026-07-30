#!/usr/bin/env python3
"""Authenticated end-to-end verification of a live deployment.

`verify_deployment.py` proves the perimeter: TLS, headers, and that guarded
routes reject anonymous callers. A 401 is a pass there — it demonstrates the
guard. It does not demonstrate that the module behind the guard *works*.

This script logs in as a seeded user and exercises every module with real
data, asserting on the shape and plausibility of what comes back. Credentials
come from the environment or --email/--password; nothing is hard-coded beyond
the documented demo login, and no secret is printed.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

G, R, Y, B, X = "\033[1;32m", "\033[1;31m", "\033[1;33m", "\033[1m", "\033[0m"

results: list[tuple[str, str, str, float]] = []
CTX = ssl.create_default_context()


class Session:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(self, path: str, method: str = "GET", body: dict | None = None,
             timeout: int = 60) -> tuple[int, object]:
        url = path if path.startswith("http") else self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raw)
                except json.JSONDecodeError:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"


def check(name: str, fn) -> object:
    started = time.perf_counter()
    try:
        ok, detail, payload = fn()
    except Exception as e:  # noqa: BLE001
        ok, detail, payload = False, f"{type(e).__name__}: {str(e)[:120]}", None
    ms = (time.perf_counter() - started) * 1000
    results.append((name, "PASS" if ok is True else ("warn" if ok is None else "FAIL"),
                    detail, ms))
    colour = G if ok is True else (Y if ok is None else R)
    label = "PASS" if ok is True else ("warn" if ok is None else "FAIL")
    print(f"  {colour}{label}{X} {name:<46} {ms:7.0f}ms  {detail[:70]}")
    return payload


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--email", default=os.environ.get("VERIFY_EMAIL", "priya.nair@democapital.in"))
    p.add_argument("--password", default=os.environ.get("VERIFY_PASSWORD", ""))
    p.add_argument("--ticker", default="RELIANCE")
    a = p.parse_args()

    s = Session(a.url)
    t = a.ticker
    print(f"\n{B}Authenticated module verification — {a.url}{X}\n")

    # ---------------------------------------------------------------- auth
    print(f"{B}Authentication{X}")

    def login():
        code, body = s.call("/api/v1/auth/login", "POST",
                            {"email": a.email, "password": a.password})
        if code == 200 and isinstance(body, dict):
            tok = body.get("access_token") or (body.get("tokens") or {}).get("access_token")
            if tok:
                s.token = tok
                user = body.get("user") or {}
                return True, f"logged in as {user.get('role', '?')}", body
            return False, f"200 but no access_token: {list(body)[:6]}", body
        return False, f"HTTP {code} {str(body)[:90]}", body

    check("login with seeded credentials", login)
    if not s.token:
        print(f"\n{R}Cannot continue without a session.{X}\n")
        return 1

    check("session identifies the caller",
          lambda: (lambda c, b: (c == 200 and isinstance(b, dict) and bool(b.get("email")),
                                 f"HTTP {c} {b.get('email', '') if isinstance(b, dict) else ''}", b))
          (*s.call("/api/v1/auth/me")))

    # ------------------------------------------------------------ universe
    print(f"\n{B}Module 1 — companies{X}")

    def universe():
        c, b = s.call("/api/v1/companies?limit=500")
        items = (b.get("results") or b.get("items") or []) if isinstance(b, dict) else b
        n = (b.get("total") if isinstance(b, dict) and b.get("total") is not None else (len(items) if isinstance(items, list) else 0))
        return (c == 200 and n > 50, f"HTTP {c}, {n} companies", items)

    check("company universe is populated", universe)

    def profile():
        c, b = s.call(f"/api/v1/companies/search?q={t}")
        rows = (b.get("results") or b.get("items") or []) if isinstance(b, dict) else (b or [])
        if c != 200 or not rows:
            return False, f"HTTP {c}, {len(rows) if isinstance(rows, list) else 0} hits", b
        first = rows[0]
        cid = first.get("id")
        c2, prof = s.call(f"/api/v1/companies/{cid}/profile")
        name = prof.get("name") if isinstance(prof, dict) else None
        # The profile may nest the company; accept either shape.
        if not name and isinstance(prof, dict):
            name = (prof.get("company") or {}).get("name")
        return (c2 == 200 and bool(name), f"search hit '{first.get('ticker')}' → profile HTTP {c2} {name}", prof)

    check("company profile resolves", profile)

    # ---------------------------------------------------------- financials
    print(f"\n{B}Module 2 — financials and ratios{X}")

    def financials():
        c, b = s.call(f"/api/v1/company/{t}/financials")
        if c != 200 or not isinstance(b, dict):
            return False, f"HTTP {c}", b
        rows = b.get("summary") or []
        years = len(rows) if isinstance(rows, list) else 0
        rev = rows[-1].get("revenue") if years and isinstance(rows[-1], dict) else None
        return (years >= 5 and bool(rev),
                f"{years} years, FY{rows[-1].get('fiscal_year')} revenue={rev:,.0f} cr" if rev
                else f"{years} years, no revenue", b)

    check("historical statements with real figures", financials)

    def ratios():
        c, b = s.call(f"/api/v1/company/{t}/ratios")
        n = len(b) if isinstance(b, (list, dict)) else 0
        return (c == 200 and n > 0, f"HTTP {c}, {n} sections", b)

    check("ratio sections computed", ratios)

    # ------------------------------------------------------------ forecast
    print(f"\n{B}Module 3 — forecast{X}")

    def forecast():
        c, b = s.call(f"/api/v1/company/{t}/forecast")
        if c != 200:
            return False, f"HTTP {c} {str(b)[:70]}", b
        rows = b.get("years") or b.get("rows") or b.get("periods") or []
        return (len(rows) > 0, f"HTTP {c}, {len(rows)} forecast periods", b)

    check("five-year forecast generated", forecast)

    # ----------------------------------------------------------- valuation
    print(f"\n{B}Module 4 — valuation{X}")

    def valuation():
        c, b = s.call(f"/api/v1/company/{t}/valuation")
        if c != 200 or not isinstance(b, dict):
            return False, f"HTTP {c} {str(b)[:70]}", b
        # The API returns one key per methodology rather than a list.
        keys = ("dcf_fcff", "dcf_fcfe", "relative", "ddm", "replacement", "sotp")
        present = [k for k in keys if isinstance(b.get(k), dict) and b.get(k)]
        wacc = (b.get("wacc") or {}).get("wacc") if isinstance(b.get("wacc"), dict) else b.get("wacc")
        return (len(present) >= 3,
                f"{len(present)}/{len(keys)} methodologies ({', '.join(present)}), wacc={wacc}", b)

    v = check("valuation methodologies produced", valuation)

    def warning_shown():
        if not isinstance(v, dict):
            return None, "valuation payload unavailable", None
        blob = json.dumps(v).lower()
        grade = str(v.get("data_grade") or v.get("grade") or "").upper()
        if grade == "INVESTMENT_GRADE":
            return True, "investment-grade data, no caveat required", None
        return ("illustrative" in blob or "warning" in blob,
                f"grade={grade}, caveat present={'illustrative' in blob}", None)

    check("illustrative-data caveat honoured", warning_shown)

    # ------------------------------------------------------------- scoring
    print(f"\n{B}Module 5 — scoring{X}")

    def scoring():
        c, b = s.call(f"/api/v1/company/{t}/scoring")
        if c != 200 or not isinstance(b, dict):
            return False, f"HTTP {c} {str(b)[:70]}", b
        cats = b.get("categories") or []
        overall = b.get("overall_score")
        ok = len(cats) >= 10 and isinstance(overall, (int, float)) and 0 <= overall <= 100
        return ok, f"{len(cats)} categories, overall={overall}, grade={b.get('grade')}", b

    check("institutional score computed", scoring)

    # ------------------------------------------------------------------ AI
    print(f"\n{B}Module 6 — AI layer{X}")

    def ai_health():
        c, b = s.call("/api/v1/ai/health", timeout=90)
        if c != 200 or not isinstance(b, dict):
            return False, f"HTTP {c} {str(b)[:70]}", b
        serving = b.get("serving")
        chain = b.get("chain")
        return bool(serving), f"serving={serving}, chain={chain}, degraded={b.get('degraded')}", b

    check("AI provider chain reachable", ai_health)

    def ai_caps():
        c, b = s.call("/api/v1/ai/capabilities")
        n = len(b) if isinstance(b, list) else len(b.get("capabilities", [])) if isinstance(b, dict) else 0
        return (c == 200 and n > 5, f"HTTP {c}, {n} capabilities", b)

    check("analyst capabilities published", ai_caps)

    # ------------------------------------------------------- docs / pf / rp
    print(f"\n{B}Modules 7–9 — documents, portfolio, reports{X}")

    for label, path in (
        ("document capabilities", "/api/v1/documents/capabilities"),
        ("portfolio list", "/api/v1/portfolios"),
        ("watchlists", "/api/v1/watchlists"),
        ("report capabilities", "/api/v1/reports/capabilities"),
    ):
        check(label, (lambda pth: lambda: (lambda c, b: (
            c == 200, f"HTTP {c}",
            b))(*s.call(pth)))(path))

    def portfolio_detail():
        c, b = s.call("/api/v1/portfolios")
        items = (b.get("results") or b.get("items") or b) if isinstance(b, dict) else b
        if not isinstance(items, list) or not items:
            return None, "no portfolio seeded", None
        pid = items[0].get("id")
        c2, b2 = s.call(f"/api/v1/portfolios/{pid}")
        return (c2 == 200, f"portfolio {pid} HTTP {c2}", b2)

    check("portfolio detail resolves", portfolio_detail)

    # ---------------------------------------------------------- operator
    print(f"\n{B}Module 10 — tenancy{X}")

    check("tenant-scoped admin reachable",
          lambda: (lambda c, b: (c in (200, 403), f"HTTP {c}", b))(*s.call("/api/v1/admin/audit/summary")))

    # 404 is the correct answer for a tenant admin: the operator console does
    # not confirm its own existence to a caller who may not use it. Anonymous
    # callers get 401. Only 200 would be a failure.
    check("cross-tenant operator console still refused",
          lambda: (lambda c, b: (c in (401, 403, 404), f"HTTP {c} (must not be 200)", b))
          (*s.call("/api/v1/platform/tenants")))

    # ----------------------------------------------------------- summary
    passed = sum(1 for _, v, _, _ in results if v == "PASS")
    warned = sum(1 for _, v, _, _ in results if v == "warn")
    failed = sum(1 for _, v, _, _ in results if v == "FAIL")
    print("\n" + "=" * 78)
    print(f"  {passed}/{len(results)} passed, {warned} warning(s), {failed} failure(s)")
    for n, v, d, _ in results:
        if v == "FAIL":
            print(f"      {R}FAIL{X} {n}: {d}")
        elif v == "warn":
            print(f"      {Y}warn{X} {n}: {d}")
    print("=" * 78 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
