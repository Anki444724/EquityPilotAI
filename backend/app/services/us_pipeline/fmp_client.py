"""Statement fetching for US listings.

Thin, cached and deliberately dull. The interesting decisions in the US
pipeline are in the mapping and the reporting unit; this only has to fetch
reliably and fail honestly.

Two things worth stating.

**`/stable`, never `/api/v3`.** FMP retired the v3 namespace on 31 August 2025
and now answers it with 403 "Legacy Endpoint … only available for legacy users
with a valid subscription prior to…". A 403 that means "this URL no longer
exists" is easily misread as a bad key, and was, earlier in this engagement.
Only `/stable` is called here.

**Caching goes through the platform cache.** Statement fetches are the slowest
part of provisioning (roughly a second each, three per company) and the data
changes quarterly at most. They share the `STATEMENTS` namespace with
`load_financials`, so a re-provision within the TTL costs nothing.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import structlog

from app.core.config import settings
from app.services.platform.cache import Namespace, cache

log = structlog.get_logger(__name__)

BASE_URL = "https://financialmodelingprep.com/stable"
TIMEOUT_SECONDS = 30.0
#: Years of history requested per statement.
#:
#: Five, not the platform's HISTORICAL_YEARS of ten, and that is a measured
#: constraint rather than a preference. FMP's free tier rejects any `limit`
#: above 5 with **HTTP 402**:
#:
#:     Premium Query Parameter: the values for 'limit' must be between 0 and 5
#:     based on your current subscription.
#:
#: Asking for ten therefore returns *nothing at all* — not a truncated five —
#: so a US company would provision with zero statements while the profile call
#: succeeded, which looks exactly like a mapping bug. Requesting the maximum
#: the plan allows is the difference between five years of history and none.
DEFAULT_LIMIT = 5

#: The ceiling the current plan enforces. Kept separate from the default so
#: raising the default on a paid plan is a one-line change with the reason
#: still recorded here.
FREE_TIER_MAX_LIMIT = 5

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class FMPStatements:
    """Company profile and the three statements, from FMP `/stable`."""

    def __init__(self, api_key: str | None = None) -> None:
        # Read from settings so the key is never passed around or logged.
        self._key = api_key or getattr(settings, "FMP_API_KEY", None)

    @property
    def configured(self) -> bool:
        return bool(self._key)

    # ------------------------------------------------------------ transport
    def _get(self, path: str, **params: Any) -> Any:
        if not self.configured:
            log.warning("FMP is not configured; US statements unavailable")
            return None

        query = urllib.parse.urlencode({**params, "apikey": self._key})
        url = f"{BASE_URL}/{path}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read()[:200].decode()
            except Exception:  # noqa: BLE001
                pass
            # The URL carries the key in its query string, so it must never be
            # logged. Path and status are enough to diagnose.
            log.warning("FMP request failed", path=path, status=exc.code,
                        detail=body[:160])
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("FMP transport error", path=path, error=str(exc)[:160])
            return None

    def _cached(self, path: str, symbol: str, **params: Any) -> Any:
        return cache.get_or_set(
            Namespace.STATEMENTS,
            lambda: self._get(path, symbol=symbol, **params),
            "fmp", path, symbol, sorted(params.items()),
        )

    # --------------------------------------------------------------- reads
    def profile(self, symbol: str) -> dict[str, Any] | None:
        rows = self._cached("profile", symbol)
        if isinstance(rows, list) and rows:
            return rows[0]
        if isinstance(rows, dict) and rows:
            return rows
        return None

    def income(self, symbol: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        return _rows(self._cached("income-statement", symbol,
                                  limit=_capped(limit)))

    def balance(self, symbol: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        return _rows(self._cached("balance-sheet-statement", symbol,
                                  limit=_capped(limit)))

    def cash_flow(self, symbol: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
        return _rows(self._cached("cash-flow-statement", symbol,
                                  limit=_capped(limit)))


def _capped(limit: int) -> int:
    """Clamp to what the plan will serve.

    Exceeding the ceiling returns 402 and therefore *no data*, so a caller
    asking for more history than the subscription allows would silently get
    none. Clamping degrades to fewer years, which is the honest failure.
    """
    return max(1, min(int(limit), FREE_TIER_MAX_LIMIT))


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise a response to a list of row dicts.

    FMP answers an unknown symbol with `[]` and an error with an object
    carrying `Error Message`. Both mean "no statements", and returning an
    empty list for each keeps that judgement out of the caller.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []
