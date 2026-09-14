"""Deterministic investment-intelligence engine (Phase 2A).

Interprets the platform's EXISTING scoring output — the ``ScoreResult`` the
``ScoringService`` already computed and the scoring citations the
``ContextBuilder`` already published — and answers investment-quality
questions directly from it. There is no provider call, no RAG, no prompt, no
new data source and no new calculation, exactly as in the Phase 1
:class:`FinancialAnswerEngine`:

* every score, grade, recommendation and category figure in an answer is a
  number the scoring engine already produced, cited under the SAME key the
  citation audit resolves (``overall_score``, ``grade``,
  ``recommendation``, ``confidence``, ``score_{category}``, plus the
  pre-existing valuation/financial evidence);
* no score, grade, recommendation or category result is recomputed or
  re-derived — the engine re-states what the scoring engine produced;
* a narrative the scoring engine did not write is never written here, and a
  narrative it DID write is quoted only when every figure in it resolves
  against the context's own citations;
* evidence that is missing is said to be missing, in so many words;
* the answer text is canonical English and is verified downstream by the
  SAME citation audit, guardrail check, annotation and language pipeline a
  provider response passes through.

The engine is a strict consumer of two existing artefacts, both already on
the :class:`GroundedContext`:

* ``citations`` — the auditable evidence the citation audit checks against;
* ``score`` — the very same run's ``ScoreResult``, read for its
  ``strongest``/``weakest`` ranking, its warnings and its category
  narratives. ``None`` when scoring did not run, which the engine reports
  as unavailable rather than filling in.
"""
from __future__ import annotations

import re

from app.domain.ai.types import Citation
from app.domain.scoring.base import CategoryScore
from app.services.ai.citation_engine import audit
from app.services.ai.context_builder import GroundedContext
from app.services.ai.financial_answer_engine import (
    FinancialAnswerEngine, _render_value,
)
from app.services.ai.guardrails import _OPINION_MARKERS
from app.services.scoring.overall_score import ScoreResult


#: A figure, the same shape the citation audit looks for in an answer.
_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d{2,}(?:\.\d+)?|\d+\.\d+)"
)


class InvestmentAnswerEngine(FinancialAnswerEngine):
    """Answers the Phase 2A investment-intelligence intents.

    Subclassing the Phase 1 engine on purpose: the intent dispatch in
    :meth:`answer`, the citation lookup and the value formatting are the
    Phase 1 machinery itself, reused rather than duplicated. This engine
    adds no data source and no calculation of its own — it reads
    ``context.citations`` and ``context.score`` and composes sentences.
    """

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _figure_backed(text: str, citations: list[Citation]) -> bool:
        """True when every figure in `text` resolves against the context's
        own citations.

        The citation audit's own figure check, run on the candidate passage
        BEFORE it is admitted. A passage the audit could not back is dropped
        — never adjusted — because moving a figure to make one fit would be
        exactly the fabrication this layer exists to prevent.
        """
        if not _NUMBER.search(text or ""):
            return True
        verdict = audit(text, citations)
        return verdict.unknown_keys == [] and verdict.uncited_numbers == []

    def _narrative_sentences(
        self, text: str, key: str, citations: list[Citation],
    ) -> list[str]:
        """The scoring engine's own category narrative, sentence by sentence.

        Each sentence that carries a figure is admitted only if the figure
        resolves against the supplied citations (the audit's own check, with
        its own tolerance), and is then marked with the category's citation
        key so the audit can see its support. Sentences the audit cannot
        back — and sentences that state an opinion the guardrail layer would
        have to moderate — are dropped, never paraphrased.
        """
        if not text:
            return []
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            if _OPINION_MARKERS.search(sentence):
                continue
            if _NUMBER.search(sentence) and not self._figure_backed(sentence, citations):
                continue
            if _NUMBER.search(sentence):
                sentence = f"{sentence.rstrip('.!?')} [{key}]."
            kept.append(sentence)
        return kept

    def _category(self, context: GroundedContext, key: str) -> CategoryScore | None:
        score = context.score
        if score is None:
            return None
        return next((c for c in score.categories if c.key == key), None)

    def _category_citation(self, context: GroundedContext, key: str) -> Citation | None:
        return self._find(context.citations, f"score_{key}")

    def _category_for_label(
        self, context: GroundedContext, label: str,
    ) -> CategoryScore | None:
        score = context.score
        if score is None:
            return None
        return next((c for c in score.categories if c.label == label), None)

    def _named_areas(
        self, context: GroundedContext, labels: list[str], *, adjective: str,
    ) -> tuple[list[str], list[Citation], list[str]]:
        """ScoreResult ``strongest``/``weakest`` labels, each with its
        category score under the existing ``score_{category}`` citation.

        A label whose category or citation is absent is not described: the
        gap is recorded in ``missing`` and the sentence is simply not there.
        """
        sentences: list[str] = []
        used: list[Citation] = []
        missing: list[str] = []
        for label in list(dict.fromkeys(labels))[:3]:
            category = self._category_for_label(context, label)
            if category is None:
                missing.append("score_" + re.sub(r"\W+", "_", label.lower()))
                continue
            citation = self._category_citation(context, category.key)
            if citation is None:
                missing.append(f"score_{category.key}")
                continue
            sentences.append(
                f"{label} is among the {adjective} areas, "
                f"scoring {_render_value(citation)} [score_{category.key}]."
            )
            used.append(citation)
        return sentences, used, missing

    def _warning_sentences(
        self, context: GroundedContext, score: ScoreResult,
    ) -> tuple[list[str], list[Citation]]:
        """The ScoreResult's warnings, minus any that carry figures.

        A warning with a number is never quoted raw — its figure would have
        no citation. The one numeric warning the scoring engine emits (low
        data confidence) is rendered from the ``confidence`` citation
        instead, so the same fact stays auditable.
        """
        sentences: list[str] = []
        used: list[Citation] = []
        low_confidence = False
        for warning in score.warnings:
            if _NUMBER.search(warning):
                if warning.strip().lower().startswith("low confidence"):
                    low_confidence = True
                continue
            text = warning.strip()
            sentences.append(
                f"The scoring engine notes: {text}"
                if text.endswith(".") else f"The scoring engine notes: {text}."
            )
        if low_confidence:
            conf = self._find(context.citations, "confidence")
            if conf is not None:
                sentences.append(
                    f"Data confidence is {_render_value(conf)} [confidence], so "
                    "the score should be read as provisional."
                )
                used.append(conf)
        return sentences, used

    def _valuation_sentences(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation]]:
        """The EXISTING valuation evidence, when the context carries it.

        Read from the citations the valuation engine already published —
        nothing here recomputes a value or an upside.
        """
        sentences: list[str] = []
        used: list[Citation] = []
        wv = self._find(context.citations, "weighted_value")
        up = self._find(context.citations, "valuation_upside")
        if wv is not None:
            sentence = (
                f"The platform's weighted intrinsic value is "
                f"{_render_value(wv)} per share [weighted_value]"
            )
            if up is not None:
                sentence += (
                    f", an upside of {_render_value(up)} versus the current "
                    "market price [valuation_upside]"
                )
            sentences.append(sentence + ".")
            used.append(wv)
            if up is not None:
                used.append(up)
        vrec = self._find(context.citations, "valuation_recommendation")
        if vrec is not None:
            sentences.append(
                f"The valuation engine's own view is '{vrec.value}' "
                "[valuation_recommendation]."
            )
            used.append(vrec)
        return sentences, used

    def _rationale_sentences(
        self, context: GroundedContext, *, with_confidence: bool = True,
    ) -> tuple[list[str], list[Citation]]:
        """The scoring engine's rationale for its recommendation.

        Re-stated from the rationale's own components rather than pasted:
        the composite score, the band it initially mapped to, and any cap
        the engine applied — valuation, balance-sheet fragility or data
        confidence — with each figure under its own existing citation. The
        cap text is read FROM ``recommendation_rationale``; no cap is
        asserted that the rationale does not record.
        """
        score = context.score
        if score is None:
            return [], []
        overall = self._find(context.citations, "overall_score")
        conf = self._find(context.citations, "confidence")
        sentences: list[str] = []
        used: list[Citation] = []

        if overall is not None:
            sentences.append(
                f"It is derived from the composite score of "
                f"{_render_value(overall)} [overall_score]."
            )
            used.append(overall)

        rationale = score.recommendation_rationale
        base_match = re.search(r"maps to (\w+)", rationale)
        base = base_match.group(1) if base_match else None
        final = score.recommendation

        caps: list[str] = []
        val = self._find(context.citations, "score_valuation")
        if "valuation scores" in rationale and val is not None:
            caps.append(
                f"the valuation category scores {_render_value(val)} "
                "[score_valuation], so the price binds regardless of "
                "business quality"
            )
            used.append(val)
        fr = self._find(context.citations, "score_financial_risk")
        if "financial risk scores" in rationale and fr is not None:
            caps.append(
                f"the financial risk category scores {_render_value(fr)} "
                "[score_financial_risk], indicating balance-sheet fragility"
            )
            used.append(fr)
        confidence_capped = "confidence is only" in rationale
        if confidence_capped and conf is not None:
            caps.append(
                f"data confidence is only {_render_value(conf)} [confidence], "
                "which does not support a directional call"
            )
            used.append(conf)

        if base and base != final:
            if caps:
                sentences.append(
                    f"The composite score initially mapped to {base}; the "
                    f"engine capped it at {final} because "
                    + "; and ".join(caps) + "."
                )
            else:
                # The rationale records a cap this engine cannot map to a
                # citation: state that a cap applied, without inventing its
                # figure.
                sentences.append(
                    f"The composite score initially mapped to {base}; the "
                    f"engine capped it at {final} for the reason recorded in "
                    "the scoring result [recommendation]."
                )
        if with_confidence and conf is not None and not confidence_capped:
            sentences.append(f"Data confidence is {_render_value(conf)} [confidence].")
            used.append(conf)
        return sentences, used

    # --------------------------------------------------------------- intents
    def _build_overall_assessment(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        score = context.score
        overall = self._find(context.citations, "overall_score")
        grade = self._find(context.citations, "grade")
        rec = self._find(context.citations, "recommendation")
        conf = self._find(context.citations, "confidence")

        if score is None or overall is None:
            return (
                [
                    f"The overall assessment cannot be fully determined from "
                    f"the available canonical scoring evidence for {name}. "
                    "The platform does not estimate a composite score or a "
                    "recommendation here — the assessment is reported as "
                    "unavailable rather than invented.",
                ],
                [],
                ["overall_score"],
            )

        used: list[Citation] = [overall]
        opening = (
            f"{name} scores {_render_value(overall)} [overall_score] on the "
            "platform's institutional scoring"
        )
        if grade is not None:
            opening += f", graded '{grade.value}' [grade]"
            used.append(grade)
        sentences = [opening + "."]

        if rec is not None:
            if conf is not None:
                sentences.append(
                    f"The scoring engine's recommendation is '{rec.value}' "
                    "[recommendation], at "
                    f"{_render_value(conf)} data confidence [confidence]."
                )
                used += [rec, conf]
            else:
                sentences.append(
                    f"The scoring engine's recommendation is '{rec.value}' "
                    "[recommendation]."
                )
                used.append(rec)

        strong, s_used, s_missing = self._named_areas(
            context, score.strongest, adjective="strongest",
        )
        weak, w_used, w_missing = self._named_areas(
            context, score.weakest, adjective="weakest",
        )
        sentences += strong + weak
        used += s_used + w_used
        missing = s_missing + w_missing

        warn, warn_used = self._warning_sentences(context, score)
        sentences += warn
        used += warn_used

        val, val_used = self._valuation_sentences(context)
        if val:
            sentences += val
            used += val_used
        else:
            sentences.append(
                "No valuation evidence is available in the current context, "
                "so the price is not part of this assessment."
            )

        sentences.append(
            "The score, grade, recommendation and category results above are "
            "the scoring engine's own outputs; they are not re-derived here."
        )
        return sentences, used, missing

    # --- the three category-score intents share a shape -------------------
    def _category_answer(
        self,
        context: GroundedContext,
        *,
        key: str,
        label: str,
        evidence: tuple[tuple[str, str], ...],
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        citation = self._category_citation(context, key)
        category = self._category(context, key)

        if citation is None:
            return (
                [
                    f"The {label.lower()} score is unavailable from the "
                    f"current scoring evidence for {name}. The platform does "
                    "not estimate a category score here.",
                ],
                [],
                [f"score_{key}"],
            )

        sentences = [
            f"The {label.lower()} score for {name} is "
            f"{_render_value(citation)} [score_{key}]."
        ]
        used: list[Citation] = [citation]
        if category is not None:
            sentences.append(
                f"The scoring engine rates the category "
                f"'{category.grade_hint}'."
            )
            sentences.append(
                f"Confidence in the category's inputs is "
                f"'{category.confidence.label}'."
            )
            sentences += self._narrative_sentences(
                category.explanation, f"score_{key}", context.citations,
            )
        for evidence_key, phrase in evidence:
            ev = self._find(context.citations, evidence_key)
            if ev is None:
                continue
            year = f" for FY{str(ev.fiscal_year)[-2:]}" if ev.fiscal_year else ""
            sentences.append(f"{phrase} is {_render_value(ev)}{year} [{evidence_key}].")
            used.append(ev)
        return sentences, used, []

    def _build_financial_quality(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        return self._category_answer(
            context, key="financial_quality", label="Financial Quality",
            evidence=(
                ("roe_avg", "Return on equity"),
                ("ebitda_margin", "The EBITDA margin"),
                ("pat_margin", "The net margin"),
            ),
        )

    def _build_growth_quality(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        sentences, used, missing = self._category_answer(
            context, key="growth_quality", label="Growth Quality",
            evidence=(
                ("revenue_growth", "The platform's computed year-on-year "
                 "revenue growth"),
                ("pat_growth", "The platform's computed year-on-year "
                 "profit growth"),
            ),
        )
        forecast = self._find(context.citations, "forecast_revenue_cagr")
        if forecast is not None:
            sentences.append(
                f"The forecast projects revenue CAGR of "
                f"{_render_value(forecast)} [forecast_revenue_cagr]."
            )
            used.append(forecast)
        return sentences, used, missing

    def _build_financial_risk(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        return self._category_answer(
            context, key="financial_risk", label="Financial Risk",
            evidence=(
                ("net_debt_ebitda", "Net debt to EBITDA"),
                ("interest_coverage", "Interest coverage"),
                ("current_ratio", "The current ratio"),
                ("altman_z", "The Altman Z-score"),
                ("gross_debt", "Gross debt"),
                ("net_debt", "Net debt"),
            ),
        )

    def _area_paragraph(
        self, context: GroundedContext, labels: list[str], *, adjective: str,
    ) -> tuple[list[str], list[Citation], list[str]]:
        """One ranked area at a time: the scoring engine's own narrative
        when the audit backs every figure in it, otherwise its score alone."""
        sentences: list[str] = []
        used: list[Citation] = []
        missing: list[str] = []
        for label in list(dict.fromkeys(labels))[:3]:
            category = self._category_for_label(context, label)
            if category is None:
                missing.append("score_" + re.sub(r"\W+", "_", label.lower()))
                continue
            citation = self._category_citation(context, category.key)
            if citation is None:
                missing.append(f"score_{category.key}")
                continue
            narrative = self._narrative_sentences(
                category.explanation, f"score_{category.key}", context.citations,
            )
            if narrative:
                sentences += narrative
            else:
                sentences.append(
                    f"{label} is among the {adjective} areas, scoring "
                    f"{_render_value(citation)} [score_{category.key}]."
                )
            if citation not in used:
                used.append(citation)
        return sentences, used, missing

    def _build_strengths(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        score = context.score
        if score is None or not score.strongest:
            return (
                [
                    f"The strongest areas cannot be listed from the current "
                    f"scoring evidence for {name}. The platform does not "
                    "rank categories it has not scored.",
                ],
                [],
                [],
            )

        sentences = [
            f"The scoring engine ranks these areas highest for {name}:",
        ]
        area, used, missing = self._area_paragraph(
            context, score.strongest, adjective="strongest",
        )
        sentences += area
        sentences.append(
            "These are the platform's computed category results; no "
            "qualitative judgement is added."
        )
        return sentences, used, missing

    def _build_weaknesses(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        score = context.score
        if score is None or not score.weakest:
            return (
                [
                    f"The weaknesses cannot be listed from the current "
                    f"scoring evidence for {name}. The platform does not "
                    "rank categories it has not scored.",
                ],
                [],
                [],
            )

        sentences = [f"The scoring engine ranks these areas lowest for {name}:"]
        area, used, missing = self._area_paragraph(
            context, score.weakest, adjective="weakest",
        )
        sentences += area
        warn, warn_used = self._warning_sentences(context, score)
        sentences += warn
        used += warn_used
        sentences.append(
            "These are the platform's computed category results and the "
            "scoring engine's own warnings; no weakness is added."
        )
        return sentences, used, missing

    def _build_investment_case(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        score = context.score
        overall = self._find(context.citations, "overall_score")

        if score is None or overall is None:
            return (
                [
                    f"The investment case cannot be assembled from the "
                    f"available canonical scoring evidence for {name}. No "
                    "score, category result or recommendation is invented "
                    "to fill the gap.",
                ],
                [],
                ["overall_score"],
            )

        used: list[Citation] = [overall]
        sentences = [
            f"The investment case below is assembled deterministically from "
            f"the platform's own scoring outputs for {name}.",
            f"Composite: {name} scores {_render_value(overall)} [overall_score].",
            "Why consider:",
        ]

        strong, s_used, _ = self._named_areas(
            context, score.strongest[:2], adjective="strongest",
        )
        sentences += strong
        used += s_used

        # Category scores the strongest list does not already cover.
        covered = {
            c.key for c in (self._category_for_label(context, l)
                            for l in score.strongest[:2]) if c
        }
        for key, label in (("financial_quality", "Financial quality"),
                           ("growth_quality", "Growth quality")):
            if key in covered:
                continue
            citation = self._category_citation(context, key)
            if citation is None:
                continue
            sentences.append(
                f"{label} scores {_render_value(citation)} [score_{key}]."
            )
            used.append(citation)

        val, val_used = self._valuation_sentences(context)
        if val:
            sentences += val
            used += val_used
        else:
            sentences.append(
                "No valuation evidence is available in the current context, "
                "so no price or upside is stated."
            )

        sentences.append("Risks / what can go wrong:")
        weak, w_used, _ = self._named_areas(
            context, score.weakest[:2], adjective="weakest",
        )
        sentences += weak
        used += w_used

        fr = self._category_citation(context, "financial_risk")
        if fr is not None and "financial_risk" not in {
            c.key for c in (self._category_for_label(context, l)
                            for l in score.weakest[:2]) if c
        }:
            sentences.append(
                f"Financial risk scores {_render_value(fr)} [score_financial_risk]."
            )
            used.append(fr)

        warn, warn_used = self._warning_sentences(context, score)
        sentences += warn
        used += warn_used

        sentences.append("Bottom line:")
        rec = self._find(context.citations, "recommendation")
        conf = self._find(context.citations, "confidence")
        if rec is not None:
            if conf is not None:
                sentences.append(
                    f"The scoring engine's recommendation is '{rec.value}' "
                    "[recommendation], at "
                    f"{_render_value(conf)} data confidence [confidence]."
                )
                used += [rec, conf]
            else:
                sentences.append(
                    f"The scoring engine's recommendation is '{rec.value}' "
                    "[recommendation]."
                )
                used.append(rec)
        rat, rat_used = self._rationale_sentences(
            context, with_confidence=rec is None or conf is None,
        )
        sentences += rat
        used += rat_used
        return sentences, used, []

    def _build_recommendation(
        self, context: GroundedContext,
    ) -> tuple[list[str], list[Citation], list[str]]:
        name = context.name
        score = context.score
        rec = self._find(context.citations, "recommendation")

        if rec is None or score is None:
            return (
                [
                    f"The recommendation is unavailable from the current "
                    f"scoring evidence for {name}. The platform does not "
                    "state a recommendation it has not computed.",
                ],
                [],
                ["recommendation"],
            )

        sentences = [
            f"The scoring engine's recommendation for {name} is "
            f"'{rec.value}' [recommendation].",
        ]
        used: list[Citation] = [rec]

        rat, rat_used = self._rationale_sentences(context)
        sentences += rat
        used += rat_used

        strong, s_used, _ = self._named_areas(
            context, score.strongest[:1], adjective="strongest",
        )
        weak, w_used, _ = self._named_areas(
            context, score.weakest[:1], adjective="weakest",
        )
        sentences += strong + weak
        used += s_used + w_used

        warn, warn_used = self._warning_sentences(context, score)
        sentences += warn
        used += warn_used

        sentences.append(
            "This is the platform's scoring output, stated as the engine "
            "computed it; it is not a fresh call."
        )
        return sentences, used, []
