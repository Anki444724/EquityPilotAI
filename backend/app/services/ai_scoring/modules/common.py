"""Shared assembly for the ten module scorers.

One place builds a :class:`ModuleScore` from a factor list, so the weighted
mean, the narrative and the missing-factor accounting are identical across all
ten modules. Ten copies of that arithmetic is ten opportunities for one module
to aggregate slightly differently from the others — the kind of defect that
never raises and never looks wrong.
"""
from __future__ import annotations

from typing import Sequence

from app.domain.ai_scoring.framework import Module, MODULE_LABELS, MODULE_WEIGHTS
from app.domain.ai_scoring.types import (
    FactorScore, ModuleScore, aggregate_factors,
)


def narrate(label: str, factors: Sequence[FactorScore], score: float) -> str:
    """Deterministic module narrative built from its own factors.

    Never model-written. This string is what appears in the panel when no LLM
    is reachable, and it must be a complete explanation on its own — the AI
    commentary is an addition, not a dependency.
    """
    assessed = [f for f in factors if not f.is_missing]
    if not assessed:
        return (f"{label} could not be assessed: none of its inputs were "
                "observable for this company.")

    verdict = (
        "strong" if score >= 8.0 else
        "good" if score >= 6.5 else
        "adequate" if score >= 5.0 else
        "weak" if score >= 3.5 else "poor"
    )
    best = max(assessed, key=lambda f: f.score)
    worst = min(assessed, key=lambda f: f.score)

    parts = [f"{label} is {verdict} at {score:.1f}/10."]
    parts.append(f"Strongest input — {best.label}: {best.reason}")
    if worst.key != best.key:
        parts.append(f"Weakest input — {worst.label}: {worst.reason}")

    missing = [f.label for f in factors if f.is_missing]
    if missing:
        shown = ", ".join(missing[:4])
        more = f" and {len(missing) - 4} more" if len(missing) > 4 else ""
        parts.append(
            f"Not assessed ({len(missing)} of {len(factors)} inputs): "
            f"{shown}{more}. These scored the neutral midpoint and reduced "
            "the module's coverage rather than its score."
        )
    return " ".join(parts)


def build_module(
    module: Module,
    factors: Sequence[FactorScore],
    *,
    ai_commentary: str | None = None,
) -> ModuleScore:
    """Assemble a module result from its factors."""
    ordered = tuple(factors)
    score = aggregate_factors(ordered)
    label = MODULE_LABELS[module]
    return ModuleScore(
        key=module.value,
        label=label,
        weight=MODULE_WEIGHTS[module],
        score=score,
        factors=ordered,
        reason=narrate(label, ordered, score),
        ai_commentary=ai_commentary,
    )


def cagr(first: float | None, last: float | None, years: int) -> float | None:
    """Compound annual growth rate over ``years`` periods.

    Returns ``None`` when the base is non-positive. A CAGR from a loss or a
    zero base is arithmetically undefined, and the usual workaround — taking
    the absolute value — produces a growth rate with the wrong sign for a
    company recovering from a loss, which is precisely the case where the
    number would be read most eagerly.
    """
    if first is None or last is None or years <= 0:
        return None
    if first <= 0:
        return None
    ratio = last / first
    if ratio <= 0:
        return None
    return ratio ** (1.0 / years) - 1.0


def series_cagr(values: list[float | None]) -> float | None:
    """CAGR across a trailing series, ignoring gaps at either end."""
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(present) < 2:
        return None
    (first_i, first_v), (last_i, last_v) = present[0], present[-1]
    return cagr(first_v, last_v, last_i - first_i)


def consistency(values: list[float | None], *, higher_is_better: bool = True) -> float | None:
    """Share of period-on-period moves that went the desired way, 0-1."""
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return None
    deltas = [present[i] - present[i - 1] for i in range(1, len(present))]
    moved = [d for d in deltas if abs(d) > 1e-9]
    if not moved:
        return 0.5
    good = sum(1 for d in moved if (d > 0) == higher_is_better)
    return good / len(moved)
