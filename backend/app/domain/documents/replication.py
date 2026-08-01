"""Domain rules for hybrid volume-primary / bucket-secondary storage.

The architecture this encodes, and why each part is the way it is.

**The volume stays authoritative.** Reads come from it, writes land on it
first, and nothing is considered stored until it is on the volume with a
verified checksum. Object storage is a replica — valuable, but not yet
trusted with 283 MB of irreplaceable filings, because it is a Railway beta
wrapping a third party.

**Replication is asynchronous and never blocks an upload.** A user waiting on
an S3 round trip to be told their PDF was accepted is a worse product, and a
bucket outage would otherwise become an upload outage. The consequence is that
a document is briefly `PENDING` — genuinely unreplicated — and the dashboard
says so rather than implying a durability the system does not yet have.

**Verification is by read-back, not by trusting a 200.** A put that succeeds
and stores nothing is rare; discovering it when a user downloads a filing
months later is not acceptable. Every replica is re-read and its SHA256
compared against the value the database recorded at upload.

**A mismatch is never repaired silently.** It is recorded as `MISMATCH`, it
alerts, and the replica is treated as absent. Two differing copies of a
financial filing is a correctness incident, not a retry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class ReplicationState(StrEnum):
    """Where a document's secondary copy stands."""

    #: Never attempted — the default for every existing document.
    PENDING = "pending"
    #: Copy in flight.
    REPLICATING = "replicating"
    #: Copied and the read-back SHA256 matched.
    VERIFIED = "verified"
    #: Copy attempt failed. Retryable.
    FAILED = "failed"
    #: Copied, but the read-back SHA256 disagreed. **Not** retryable without
    #: an operator: silently overwriting is how one of two conflicting copies
    #: of a filing becomes the survivor by accident.
    MISMATCH = "mismatch"
    #: Deliberately excluded, e.g. a document whose primary bytes are gone.
    SKIPPED = "skipped"


#: States that will not change without another replication pass.
TERMINAL_STATES: frozenset[ReplicationState] = frozenset({
    ReplicationState.VERIFIED, ReplicationState.MISMATCH,
    ReplicationState.SKIPPED,
})

#: States a background pass should pick up.
REPLICABLE_STATES: frozenset[ReplicationState] = frozenset({
    ReplicationState.PENDING, ReplicationState.FAILED,
})

#: Attempts before a document stops being retried automatically.
#:
#: Bounded because a permanently unreplicable document — bad key, deleted
#: primary — would otherwise consume the queue on every pass forever, hiding
#: the documents that could still succeed.
MAX_REPLICATION_ATTEMPTS = 5


def should_retry(state: str | ReplicationState, attempts: int) -> bool:
    try:
        current = ReplicationState(state)
    except ValueError:
        return False
    if current is ReplicationState.MISMATCH:
        # Never automatic. A checksum disagreement is an incident.
        return False
    return current in REPLICABLE_STATES and attempts < MAX_REPLICATION_ATTEMPTS


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
#: Document types that must never be deleted by any automated process.
#:
#: These are the research corpus. Everything the platform says about a company
#: is ultimately traceable to one of them, so a retention sweep that removes
#: one silently invalidates every citation pointing at it.
PROTECTED_DOC_TYPES: frozenset[str] = frozenset({
    "annual_report",
    "quarterly_report",
    "investor_presentation",
    "conference_call",
    "esg_report",
    "credit_rating",
})


def is_protected(doc_type: str | None) -> bool:
    """True when automated deletion must refuse to touch this document."""
    return (doc_type or "").strip().lower() in PROTECTED_DOC_TYPES


# ---------------------------------------------------------------------------
# Health thresholds
# ---------------------------------------------------------------------------
#: Volume utilisation at which an operator is alerted.
#:
#: 80% of a 500 MB volume leaves 100 MB — roughly four large annual reports.
#: Alerting later than this leaves no time to act before collection halts.
VOLUME_WARN_RATIO = 0.80
VOLUME_CRITICAL_RATIO = 0.92

#: A replication backlog above this is reported as unhealthy rather than busy.
REPLICATION_BACKLOG_WARN = 50

#: How stale the last successful replication may be before it is suspicious.
#: Twice the scheduler interval, so one missed run is tolerated and two are not.
REPLICATION_STALE_AFTER = timedelta(hours=2)


class HealthLevel(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class HealthSignal:
    """One assessed dimension of storage health."""

    name: str
    level: HealthLevel
    detail: str
    value: float | int | str | None = None

    @property
    def is_alerting(self) -> bool:
        return self.level in (HealthLevel.WARNING, HealthLevel.CRITICAL)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "level": self.level.value,
            "detail": self.detail, "value": self.value,
            "alerting": self.is_alerting,
        }


def assess_volume(used_bytes: int, total_bytes: int) -> HealthSignal:
    """Volume utilisation against the warning thresholds."""
    if total_bytes <= 0:
        return HealthSignal("volume_usage", HealthLevel.WARNING,
                            "volume size unknown", None)
    ratio = used_bytes / total_bytes
    percent = round(ratio * 100, 1)
    if ratio >= VOLUME_CRITICAL_RATIO:
        level, detail = HealthLevel.CRITICAL, (
            f"volume {percent}% full — automatic collection will stop"
        )
    elif ratio >= VOLUME_WARN_RATIO:
        level, detail = HealthLevel.WARNING, f"volume {percent}% full"
    else:
        level, detail = HealthLevel.OK, f"volume {percent}% full"
    return HealthSignal("volume_usage", level, detail, percent)


def assess_replication(
    pending: int, failed: int, mismatched: int,
    last_success: datetime | None, now: datetime | None = None,
) -> list[HealthSignal]:
    """Every replication dimension the dashboard reports."""
    now = now or datetime.now(timezone.utc)
    signals: list[HealthSignal] = []

    signals.append(HealthSignal(
        "replication_queue",
        HealthLevel.WARNING if pending > REPLICATION_BACKLOG_WARN
        else HealthLevel.OK,
        f"{pending} document(s) awaiting replication", pending,
    ))
    signals.append(HealthSignal(
        "replication_failures",
        HealthLevel.WARNING if failed else HealthLevel.OK,
        f"{failed} document(s) failed to replicate", failed,
    ))
    # A checksum disagreement is critical, not a warning: it means two copies
    # of a filing differ and one of them is wrong.
    signals.append(HealthSignal(
        "checksum_verification",
        HealthLevel.CRITICAL if mismatched else HealthLevel.OK,
        (f"{mismatched} SHA256 mismatch(es) — replicas differ from the "
         f"primary" if mismatched else "all verified replicas match"),
        mismatched,
    ))

    if last_success is None:
        signals.append(HealthSignal(
            "last_replication", HealthLevel.WARNING,
            "no successful replication recorded yet", None,
        ))
    else:
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        age = now - last_success
        stale = age > REPLICATION_STALE_AFTER and pending > 0
        signals.append(HealthSignal(
            "last_replication",
            HealthLevel.WARNING if stale else HealthLevel.OK,
            (f"last successful replication {int(age.total_seconds() // 60)} "
             f"minute(s) ago"),
            last_success.isoformat(),
        ))
    return signals


# ---------------------------------------------------------------------------
# Cutover policy
# ---------------------------------------------------------------------------
#: Consecutive days of clean replication before object storage may be promoted.
PROMOTION_MIN_DAYS = 30


@dataclass(frozen=True, slots=True)
class PromotionCriterion:
    name: str
    met: bool
    detail: str

    def as_dict(self) -> dict:
        return {"criterion": self.name, "met": self.met, "detail": self.detail}


def assess_promotion(
    *,
    clean_days: float,
    mismatches: int,
    failures: int,
    unreplicated: int,
    total: int,
) -> tuple[bool, list[PromotionCriterion]]:
    """Is object storage ready to become primary?

    Deliberately conservative and entirely mechanical. The decision to trust a
    beta service with the only copy of a research corpus should not rest on
    anybody's impression that it has "been fine".
    """
    criteria = [
        PromotionCriterion(
            f"{PROMOTION_MIN_DAYS} consecutive days of successful replication",
            clean_days >= PROMOTION_MIN_DAYS,
            f"{clean_days:.1f} day(s) observed",
        ),
        PromotionCriterion(
            "zero checksum mismatches", mismatches == 0,
            f"{mismatches} mismatch(es) recorded",
        ),
        PromotionCriterion(
            "zero replication failures outstanding", failures == 0,
            f"{failures} failure(s) outstanding",
        ),
        PromotionCriterion(
            "every document replicated and verified", unreplicated == 0,
            f"{unreplicated} of {total} document(s) not yet verified",
        ),
    ]
    return all(c.met for c in criteria), criteria
