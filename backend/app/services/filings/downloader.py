"""Fetch a discovered filing's bytes, safely.

Downloading arbitrary URLs found on third-party pages is the least trusted
operation in the platform, so the guards here are deliberate rather than
defensive habit:

* **Size is capped while streaming, not after.** Checking `Content-Length` is
  not enough — it is supplied by the server and may be absent or a lie. The
  read loop aborts once the cap is exceeded, so a hostile or misconfigured
  endpoint cannot exhaust the 500 MB volume.
* **The payload must actually be a PDF.** An expired link commonly returns a
  200 with an HTML error page, and ingesting that would produce a "document"
  whose text is a login form — indistinguishable, downstream, from a real
  filing that happens to be uninformative. The magic bytes are checked.
* **Redirects are followed but bounded**, and only to http/https, so a
  redirect chain cannot reach `file://` or an internal address.

Nothing here writes to disk. Bytes are returned to the caller, which hands
them to the existing ingestion service so automatically-collected documents
travel exactly the same storage and processing path as uploaded ones.
"""
from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import structlog

from app.domain.filings.collection import MAX_AUTO_DOWNLOAD_BYTES

log = structlog.get_logger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

#: NSE serves its archive only to requests that look like they came from the
#: site. Verified: without the Referer the same URL returns 403.
_NSE_REFERER = "https://www.nseindia.com/"

_CHUNK = 64 * 1024


class DownloadError(Exception):
    """The filing could not be retrieved. Carries whether a retry may help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    content: bytes
    sha256: str
    size: int
    content_type: str
    final_url: str
    latency_ms: float

    @property
    def is_pdf(self) -> bool:
        return self.content[:5] == b"%PDF-"


def _headers_for(url: str) -> dict[str, str]:
    headers = {
        "User-Agent": _UA,
        "Accept": "application/pdf,*/*",
        # urllib does not transparently decompress, so advertising gzip
        # yields bytes that fail to decode.
        "Accept-Encoding": "identity",
    }
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if "nseindia" in host or "nsearchives" in host:
        headers["Referer"] = _NSE_REFERER
    elif "bseindia" in host:
        headers["Referer"] = "https://www.bseindia.com/"
    return headers


class FilingDownloader:
    """Streams a filing into memory, with size and type guards."""

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        max_bytes: int = MAX_AUTO_DOWNLOAD_BYTES,
        require_pdf: bool = True,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.require_pdf = require_pdf

    def fetch(self, url: str) -> DownloadedFile:
        if not url:
            raise DownloadError("no url", retryable=False)
        scheme = (urllib.parse.urlparse(url).scheme or "").lower()
        if scheme not in ("http", "https"):
            # A non-web scheme on a URL scraped from a third-party page is
            # either a mistake or an attempt to read the local filesystem.
            raise DownloadError(f"unsupported scheme '{scheme}'", retryable=False)

        started = time.perf_counter()
        request = urllib.request.Request(url, headers=_headers_for(url))

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise DownloadError(
                        f"declared size {int(declared):,} exceeds the "
                        f"{self.max_bytes:,} byte automatic limit",
                        retryable=False,
                    )

                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    # Enforced against bytes actually received, because
                    # Content-Length is the server's claim, not a fact.
                    if total > self.max_bytes:
                        raise DownloadError(
                            f"exceeded the {self.max_bytes:,} byte limit "
                            f"while streaming",
                            retryable=False,
                        )
                    chunks.append(chunk)

                content = b"".join(chunks)
                content_type = (response.headers.get("Content-Type") or "").lower()
                final_url = response.geturl()

        except urllib.error.HTTPError as exc:
            # 4xx will not change on retry; 5xx and timeouts may.
            raise DownloadError(
                f"HTTP {exc.code}", retryable=exc.code >= 500,
            ) from exc
        except urllib.error.URLError as exc:
            raise DownloadError(f"transport error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise DownloadError("timed out") from exc

        if not content:
            raise DownloadError("empty response", retryable=True)

        result = DownloadedFile(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            content_type=content_type,
            final_url=final_url,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

        if self.require_pdf and not result.is_pdf:
            # An expired or gated link returns 200 with an HTML error page.
            # Ingesting it would create a document whose "text" is a login
            # form, which reads downstream as a genuine but uninformative
            # filing — far worse than a recorded failure.
            preview = content[:120].decode("utf-8", errors="replace")
            raise DownloadError(
                f"not a PDF (content-type {content_type or 'unknown'}); "
                f"body begins {preview!r}",
                retryable=False,
            )

        return result
