"""OCR policy and engine.

The brief's requirement is precise: *automatically determine whether OCR is
required, and do not OCR machine-readable PDFs unnecessarily*. That is a cost
and a fidelity decision at once — OCR of a page that already has a text layer
is both slower and worse, because recognition introduces errors where none
existed.

The decision is made per page, not per document, because Indian annual reports
routinely interleave a typeset MD&A with scanned, signed auditor certificates.
A document-level flag would either miss the scans or corrupt the typeset text.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from app.domain.documents.types import OcrUnavailable, TextSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OcrPolicy:
    """Thresholds governing the OCR decision. Tunable, never hard-coded inline."""

    #: Below this many characters a page is treated as having no usable text.
    min_chars_per_page: int = 120
    #: Characters per 1000 pt² of page area below which the page looks empty.
    min_char_density: float = 0.06
    #: A page with at least this much image coverage is a scan candidate.
    image_area_ratio: float = 0.55
    #: Rasterisation scale. 2.0 ≈ 144 dpi, the usual accuracy/latency knee.
    render_scale: float = 2.0
    #: Skip OCR entirely above this page count unless forced (latency guard).
    max_pages: int = 400

    def needs_ocr(
        self, *, char_count: int, page_area: float, image_ratio: float
    ) -> bool:
        """Decide for a single page.

        Two independent signals, either sufficient:

        * **Sparse text.** Too few characters, absolutely or per unit area.
        * **Image-dominated.** A large raster covering the page, which is what
          a scanned page looks like to a PDF parser.

        A page that is genuinely near-blank (a section divider) will be sent to
        OCR and come back near-blank. That is wasted work but not a wrong
        answer, and it is the safe direction to err in.
        """
        if char_count < self.min_chars_per_page:
            return True
        if image_ratio >= self.image_area_ratio:
            return True
        if page_area > 0:
            density = char_count / (page_area / 1000.0)
            if density < self.min_char_density:
                return True
        return False


DEFAULT_POLICY = OcrPolicy()


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    confidence: float
    source: TextSource


class OcrEngine:
    """Tesseract wrapper with an honest availability check.

    If Tesseract is absent the engine says so rather than returning empty
    strings. A scanned annual report that silently yields no text would be
    indistinguishable from an empty filing, and the platform would go on to
    report 0% extraction coverage as though that were a fact about the company.
    """

    def __init__(self, policy: OcrPolicy | None = None, language: str = "eng") -> None:
        self.policy = policy or DEFAULT_POLICY
        self.language = language
        self._available: bool | None = None

    # -- availability -------------------------------------------------
    @property
    def available(self) -> bool:
        """True when both the Python binding and the binary are present."""
        if self._available is None:
            self._available = self._probe()
        return self._available

    def _probe(self) -> bool:
        if shutil.which("tesseract") is None:
            logger.info("OCR unavailable: tesseract binary not on PATH")
            return False
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            logger.info("OCR unavailable: %s", exc)
            return False
        return True

    def describe(self) -> dict[str, object]:
        """Status for the API, so the UI can be truthful about capability."""
        version = None
        if self.available:
            try:
                import pytesseract

                version = str(pytesseract.get_tesseract_version())
            except Exception:  # pragma: no cover - defensive
                version = "unknown"
        return {
            "available": self.available,
            "engine": "tesseract",
            "version": version,
            "language": self.language,
            "policy": {
                "min_chars_per_page": self.policy.min_chars_per_page,
                "image_area_ratio": self.policy.image_area_ratio,
                "render_scale": self.policy.render_scale,
            },
        }

    # -- recognition --------------------------------------------------
    def recognise(self, image_bytes: bytes) -> OcrResult:
        """Recognise a rasterised page.

        Confidence is the mean of Tesseract's per-word confidences, ignoring
        the -1 sentinel it emits for layout-only boxes. Words, not characters,
        because a wholly misread word usually drags several characters with it.
        """
        if not self.available:
            raise OcrUnavailable(
                "OCR was required for this page but Tesseract is not installed"
            )
        import io

        import pytesseract
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image, lang=self.language)
        confidence = self._word_confidence(image, pytesseract)
        return OcrResult(text=text, confidence=confidence, source=TextSource.OCR)

    def _word_confidence(self, image, pytesseract) -> float:
        try:
            data = pytesseract.image_to_data(
                image, lang=self.language, output_type=pytesseract.Output.DICT
            )
        except Exception:  # pragma: no cover - defensive
            return 0.0
        scores = [
            float(c) for c in data.get("conf", [])
            if str(c).lstrip("-").replace(".", "", 1).isdigit() and float(c) >= 0
        ]
        return round(sum(scores) / len(scores) / 100.0, 4) if scores else 0.0
