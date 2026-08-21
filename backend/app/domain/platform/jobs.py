"""Background job vocabulary: kinds, states, retry policy, queue arithmetic.

Modules 7 and 9 each grew their own job table (`document_jobs`,
`report_jobs`), each with its own status strings and neither with a worker.
This module is the generalisation: one queue, one state machine, one retry
policy, and the per-module tables keep their domain columns while the *queue
semantics* live in one place.

Two decisions worth stating.

**The state machine is declared as a transition table, not as `if` statements
scattered through a worker.** An illegal transition raises rather than being
silently accepted, which is how a job ends up "completed" and "retrying" at
once.

**Retry is exponential with jitter and a dead-letter terminus.** A job that
has exhausted its attempts is not deleted and not left `running` forever; it
goes to `dead_letter`, where an operator can see it. A queue that quietly
loses work is worse than one that visibly stalls.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum


class JobKind(StrEnum):
    """Every kind of work the platform performs off the request path.

    The five named in the brief, plus the housekeeping the platform needs to
    run itself.
    """

    REPORT_GENERATION = "report_generation"
    DOCUMENT_PROCESSING = "document_processing"
    EMBEDDING = "embedding"
    NOTIFICATION = "notification"
    PORTFOLIO_REFRESH = "portfolio_refresh"
    # Automated Indian filing collection.
    STORAGE_REPLICATION = "storage_replication"
    FILING_CRAWL = "filing_crawl"
    FILING_POST_PROCESS = "filing_post_process"
    # housekeeping
    ALERT_EVALUATION = "alert_evaluation"
    USAGE_ROLLUP = "usage_rollup"
    BACKUP = "backup"
    RETENTION_SWEEP = "retention_sweep"
    #: Automatic memory enrichment after a document is ingested. Runs OUTSIDE
    #: the document worker: production has crashed three times in a 1 GB
    #: container while that worker held a large PDF, and adding LLM work to
    #: the same loop would guarantee a fourth.
    MEMORY_ENRICHMENT = "memory_enrichment"
    #: Probe for investor-relations URLs. Separate from the crawl because it
    #: is a different failure mode: a crawl failure means a source was down,
    #: an IR-discovery failure means a company has no findable page.
    IR_DISCOVERY = "ir_discovery"
    #: Rescore data quality across the universe. The per-company refresh runs
    #: inside memory enrichment; this catches companies whose score changed
    #: through the passage of time alone — a filing ageing past its freshness
    #: horizon lowers the score with no new data arriving.
    QUALITY_REFRESH = "quality_refresh"
    #: Embed chunks that have no semantic vector. Self-arming: it costs one
    #: cheap COUNT when no provider is configured, and starts backfilling on
    #: its own the moment one is.
    EMBEDDING_BACKFILL = "embedding_backfill"
    #: Recalculate the ten-module AI score across the universe. Scheduled as
    #: well as filing-triggered: several modules read time-sensitive evidence
    #: (the twelve-month news window, filing freshness), so a company's score
    #: changes through the passage of time alone with no new document.
    AI_SCORE_REFRESH = "ai_score_refresh"
    #: Ingest canonical annual financials for companies in the universe that
    #: have none. Runs the same `FinancialsBackfillService` the manual
    #: `deploy/backfill_financials.py` drives, so the scheduled sweep and an
    #: on-demand run share one implementation rather than drifting apart.
    FINANCIALS_BACKFILL = "financials_backfill"
    #: Refresh quarterly results and shareholding patterns (Task 7). Drives
    #: the existing `PeriodicBackfillService` — the same throttled screener
    #: path the manual `deploy/backfill_periodic.py` script uses — so the
    #: scheduled sweep and an on-demand run share one implementation.
    PERIODIC_SYNC = "periodic_sync"
    # ---- Phase 1: the 5,000-company universe jobs -------------------------
    #: Upsert the company master from the configured source (mock | NSE+BSE
    #: masters). Batched, resumable, identity-preserving.
    COMPANY_UNIVERSE_SYNC = "company_universe_sync"
    #: Refresh persisted quotes (market_quotes) + Redis for a bounded batch,
    #: stalest-first. Never polls the universe from a request.
    PRICE_SYNC = "price_sync"
    #: Backfill daily OHLCV bars (price_history) for a bounded batch.
    HISTORICAL_PRICE_SYNC = "historical_price_sync"
    #: Re-drive symbols recorded in ingestion_failures whose backoff elapsed.
    FAILED_DATA_RETRY = "failed_data_retry"


JOB_LABELS: dict[JobKind, str] = {
    JobKind.REPORT_GENERATION: "Report generation",
    JobKind.DOCUMENT_PROCESSING: "Document processing",
    JobKind.EMBEDDING: "Embedding",
    JobKind.NOTIFICATION: "Notification",
    JobKind.PORTFOLIO_REFRESH: "Scheduled portfolio update",
    JobKind.STORAGE_REPLICATION: "Storage replication",
    JobKind.FILING_CRAWL: "Filing collection crawl",
    JobKind.FILING_POST_PROCESS: "Filing post-processing",
    JobKind.ALERT_EVALUATION: "Alert evaluation",
    JobKind.USAGE_ROLLUP: "Usage roll-up",
    JobKind.BACKUP: "Backup",
    JobKind.RETENTION_SWEEP: "Retention sweep",
    JobKind.MEMORY_ENRICHMENT: "Memory enrichment",
    JobKind.IR_DISCOVERY: "IR URL discovery",
    JobKind.QUALITY_REFRESH: "Data quality refresh",
    JobKind.EMBEDDING_BACKFILL: "Embedding backfill",
    JobKind.AI_SCORE_REFRESH: "AI score refresh",
    JobKind.FINANCIALS_BACKFILL: "Financials backfill",
    JobKind.PERIODIC_SYNC: "Quarterly & shareholding sync",
    JobKind.COMPANY_UNIVERSE_SYNC: "Company universe sync",
    JobKind.PRICE_SYNC: "Live price sync",
    JobKind.HISTORICAL_PRICE_SYNC: "Historical price sync",
    JobKind.FAILED_DATA_RETRY: "Failed data retry",
}


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"           # failed, retry scheduled
    DEAD_LETTER = "dead_letter"  # attempts exhausted, needs a human
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.SUCCEEDED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED,
})

ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED,
})

#: Legal transitions. Anything absent is a bug in the worker.
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DEAD_LETTER,
        JobStatus.CANCELLED,
    }),
    # A failed job is re-queued by the scheduler once its backoff elapses.
    JobStatus.FAILED: frozenset({
        JobStatus.QUEUED, JobStatus.DEAD_LETTER, JobStatus.CANCELLED,
    }),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.DEAD_LETTER: frozenset({JobStatus.QUEUED}),  # manual replay
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        self.current, self.target = current, target
        super().__init__(f"cannot move a job from '{current}' to '{target}'")


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: JobStatus, target: JobStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


class JobPriority(IntEnum):
    """Lower value runs first. Interactive work outranks housekeeping so a
    user waiting for a report is not stuck behind a nightly sweep."""

    INTERACTIVE = 10   # a user is watching a spinner
    HIGH = 20
    NORMAL = 50
    LOW = 80
    BACKGROUND = 100


DEFAULT_PRIORITY: dict[JobKind, JobPriority] = {
    JobKind.REPORT_GENERATION: JobPriority.INTERACTIVE,
    JobKind.DOCUMENT_PROCESSING: JobPriority.HIGH,
    JobKind.EMBEDDING: JobPriority.NORMAL,
    JobKind.NOTIFICATION: JobPriority.HIGH,
    JobKind.PORTFOLIO_REFRESH: JobPriority.LOW,
    # The nightly crawl is long-running and nobody is waiting on it, so it
    # must never sit ahead of a user's report in the queue.
    # Never ahead of user-facing work: a replica is a safety net, not a
    # product feature anybody is waiting for.
    JobKind.STORAGE_REPLICATION: JobPriority.BACKGROUND,
    JobKind.FILING_CRAWL: JobPriority.BACKGROUND,
    # Post-processing is closer to interactive: a user who sees a new filing
    # listed expects its scores to follow shortly.
    JobKind.FILING_POST_PROCESS: JobPriority.LOW,
    JobKind.ALERT_EVALUATION: JobPriority.NORMAL,
    JobKind.USAGE_ROLLUP: JobPriority.BACKGROUND,
    JobKind.BACKUP: JobPriority.BACKGROUND,
    JobKind.RETENTION_SWEEP: JobPriority.BACKGROUND,
    JobKind.MEMORY_ENRICHMENT: JobPriority.LOW,
    JobKind.IR_DISCOVERY: JobPriority.BACKGROUND,
    JobKind.QUALITY_REFRESH: JobPriority.BACKGROUND,
    JobKind.EMBEDDING_BACKFILL: JobPriority.BACKGROUND,
    JobKind.AI_SCORE_REFRESH: JobPriority.BACKGROUND,
    JobKind.FINANCIALS_BACKFILL: JobPriority.BACKGROUND,
    JobKind.PERIODIC_SYNC: JobPriority.BACKGROUND,
    JobKind.COMPANY_UNIVERSE_SYNC: JobPriority.BACKGROUND,
    JobKind.PRICE_SYNC: JobPriority.BACKGROUND,
    JobKind.HISTORICAL_PRICE_SYNC: JobPriority.BACKGROUND,
    JobKind.FAILED_DATA_RETRY: JobPriority.BACKGROUND,
}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with a cap and deterministic jitter.

    Jitter is derived from the job's identity rather than from `random`, so a
    test can assert the exact schedule while a thousand simultaneous failures
    still spread out instead of retrying in lockstep.
    """

    max_attempts: int = 3
    base_seconds: float = 5.0
    factor: float = 4.0
    max_seconds: float = 900.0
    jitter_fraction: float = 0.20

    def backoff_seconds(self, attempt: int, seed: str = "") -> float:
        """Delay before attempt number `attempt` (1-based: the delay after the
        first failure is `backoff_seconds(1)`)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = min(self.base_seconds * (self.factor ** (attempt - 1)), self.max_seconds)
        if self.jitter_fraction <= 0 or not seed:
            return round(raw, 3)
        # Deterministic ±jitter from a hash of (seed, attempt).
        digest = hashlib.sha256(f"{seed}:{attempt}".encode()).digest()
        unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF   # 0..1
        offset = (unit * 2 - 1) * self.jitter_fraction          # -j..+j
        return round(max(0.0, raw * (1 + offset)), 3)

    def next_run_at(self, now: datetime, attempt: int, seed: str = "") -> datetime:
        return now + timedelta(seconds=self.backoff_seconds(attempt, seed))

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts

    def outcome_after_failure(self, attempts: int) -> JobStatus:
        """Where a job lands when an attempt fails."""
        return JobStatus.DEAD_LETTER if self.exhausted(attempts) else JobStatus.FAILED


#: Per-kind policies. Report generation is retried least: it is expensive, and
#: a deterministic failure will fail identically the second time. Notifications
#: are retried most: the failure is usually a transient network hiccup.
RETRY_POLICIES: dict[JobKind, RetryPolicy] = {
    JobKind.REPORT_GENERATION: RetryPolicy(max_attempts=2, base_seconds=5),
    JobKind.DOCUMENT_PROCESSING: RetryPolicy(max_attempts=3, base_seconds=10),
    JobKind.EMBEDDING: RetryPolicy(max_attempts=3, base_seconds=5),
    JobKind.NOTIFICATION: RetryPolicy(max_attempts=5, base_seconds=2, factor=3),
    JobKind.PORTFOLIO_REFRESH: RetryPolicy(max_attempts=3, base_seconds=30),
    # A crawl that fails is usually the exchange rate-limiting us, and the
    # right response is to back off substantially rather than hammer it.
    # Object storage being briefly unavailable is the common failure, and the
    # volume copy is authoritative throughout, so there is no urgency.
    JobKind.STORAGE_REPLICATION: RetryPolicy(max_attempts=3, base_seconds=120),
    JobKind.FILING_CRAWL: RetryPolicy(max_attempts=2, base_seconds=300),
    JobKind.FILING_POST_PROCESS: RetryPolicy(max_attempts=3, base_seconds=30),
    JobKind.ALERT_EVALUATION: RetryPolicy(max_attempts=2, base_seconds=30),
    JobKind.USAGE_ROLLUP: RetryPolicy(max_attempts=3, base_seconds=60),
    JobKind.BACKUP: RetryPolicy(max_attempts=2, base_seconds=120),
    JobKind.RETENTION_SWEEP: RetryPolicy(max_attempts=2, base_seconds=120),
    # Long backoff: the usual failure is an LLM rate limit, and retrying in
    # five seconds simply burns the remaining quota.
    JobKind.MEMORY_ENRICHMENT: RetryPolicy(max_attempts=3, base_seconds=180),
    # One retry only: a company with no findable IR page will not acquire one
    # by being probed again in five minutes.
    JobKind.IR_DISCOVERY: RetryPolicy(max_attempts=2, base_seconds=600),
    JobKind.QUALITY_REFRESH: RetryPolicy(max_attempts=2, base_seconds=300),
    # Long backoff: the usual failure is an exhausted quota, and retrying in
    # thirty seconds simply burns what is left.
    JobKind.EMBEDDING_BACKFILL: RetryPolicy(max_attempts=2, base_seconds=900),
    # Scoring is pure arithmetic over the database, so a failure is a bug or
    # a transient connection loss rather than a rate limit. Retry quickly,
    # twice.
    JobKind.AI_SCORE_REFRESH: RetryPolicy(max_attempts=3, base_seconds=60),
    # The usual failure is the source provider rate-limiting or returning a
    # transient error. Back off substantially so a retry is not a fresh
    # request into the same throttle. The sweep is resumable by construction
    # — the target set is recomputed from the database — so a short run is
    # not a lost run.
    JobKind.FINANCIALS_BACKFILL: RetryPolicy(max_attempts=2, base_seconds=900),
    # Same failure profile as the financials backfill: the usual failure is
    # the source provider rate-limiting us, and the sweep is resumable by
    # construction (stalest-first selection), so back off substantially.
    JobKind.PERIODIC_SYNC: RetryPolicy(max_attempts=2, base_seconds=900),
    # ---- Phase 1 ----------------------------------------------------------
    # Universe sync is idempotent and resumable (stats.next_index), so a retry
    # continues rather than repeats; back off hard because the usual failure
    # is an exchange master rate-limiting us.
    JobKind.COMPANY_UNIVERSE_SYNC: RetryPolicy(max_attempts=3, base_seconds=600),
    # Quote batches are short and the next schedule picks up fresh work
    # anyway; retry quickly but not instantly.
    JobKind.PRICE_SYNC: RetryPolicy(max_attempts=3, base_seconds=60),
    JobKind.HISTORICAL_PRICE_SYNC: RetryPolicy(max_attempts=2, base_seconds=600),
    JobKind.FAILED_DATA_RETRY: RetryPolicy(max_attempts=3, base_seconds=120),
}


def policy_for(kind: JobKind) -> RetryPolicy:
    return RETRY_POLICIES[kind]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def idempotency_key(kind: JobKind, tenant_id: int | None, payload: dict) -> str:
    """A stable key for "this exact work, already asked for".

    Enqueuing the same work twice while the first copy is still pending should
    return the existing job, not create a duplicate. Sorting the payload keys
    makes the hash independent of dict ordering.
    """
    parts = [kind.value, str(tenant_id or 0)]
    for key in sorted(payload):
        parts.append(f"{key}={payload[key]!r}")
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Queue health
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QueueDepth:
    """A snapshot of the queue, as the monitoring endpoint reports it."""

    queued: int = 0
    running: int = 0
    failed: int = 0
    dead_letter: int = 0
    succeeded_24h: int = 0
    oldest_queued_seconds: float = 0.0
    p50_duration_ms: float = 0.0
    p95_duration_ms: float = 0.0
    by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def backlog(self) -> int:
        return self.queued + self.running + self.failed

    @property
    def is_healthy(self) -> bool:
        """Healthy means: nothing in the dead-letter queue, and nothing has
        been waiting more than five minutes. Both thresholds are stated here
        rather than in the endpoint so the dashboard and the alert agree."""
        return self.dead_letter == 0 and self.oldest_queued_seconds < 300


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """A recurring job. Interval-based rather than cron: the platform has no
    schedule that needs calendar semantics, and an interval is trivially
    testable."""

    kind: JobKind
    every_seconds: int
    description: str
    enabled: bool = True

    def due(self, last_run: datetime | None, now: datetime) -> bool:
        if not self.enabled:
            return False
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= self.every_seconds


#: The platform's standing schedule.
SCHEDULES: tuple[ScheduleSpec, ...] = (
    ScheduleSpec(
        # Every 10 minutes: frequent enough that a new upload is replicated
        # while it is still topical, infrequent enough that a bucket outage
        # does not generate a retry storm.
        JobKind.STORAGE_REPLICATION, 600,
        "Copy unreplicated documents to object storage and verify SHA256.",
    ),
    ScheduleSpec(
        # Twice daily, 260 companies per pass.
        #
        # 2 x 260 = 520 >= the 500-company universe, so every company is
        # checked within 24 hours even if one pass is short. A single daily
        # pass at 25 companies left 340 of 501 never crawled, because the
        # WEEKLY tier re-queued the head of the list before the tail was
        # reached.
        JobKind.FILING_CRAWL, 12 * 3600,
        "Crawl investor-relations sites, NSE and BSE for new filings and "
        "ingest anything not already held.",
    ),
    ScheduleSpec(
        # Daily. Probing is cheap and IR pages move, but not hourly.
        JobKind.IR_DISCOVERY, 24 * 3600,
        "Discover and store investor-relations URLs for companies that have "
        "none, so the brief's Priority-1 source stops being empty.",
    ),
    ScheduleSpec(
        # Runs far more often than the crawl: ingestion is asynchronous, so
        # documents finish indexing minutes to hours after they are collected
        # and the rescore must follow them rather than the crawl.
        JobKind.FILING_POST_PROCESS, 900,
        "Rescore and notify for filings whose documents have finished "
        "indexing.",
    ),
    ScheduleSpec(
        JobKind.PORTFOLIO_REFRESH, 24 * 3600,
        "Revalue every portfolio and write a dated snapshot.",
    ),
    ScheduleSpec(
        JobKind.ALERT_EVALUATION, 3600,
        "Evaluate the alert rule set across every portfolio and watchlist.",
    ),
    ScheduleSpec(
        JobKind.MEMORY_ENRICHMENT, 3600,
        "Safety net: enrich any company whose documents have outrun its "
        "memory. The per-document enqueue is the primary path; this catches "
        "an enqueue lost to a crash, and drains the LLM-backed stages that "
        "were skipped when a provider was rate-limited.",
    ),
    ScheduleSpec(
        # Every 30 minutes. Cheap when idle — one COUNT against an indexed
        # column — and it means a corpus starts embedding itself within half
        # an hour of a key being added, with no deploy and no manual step.
        JobKind.EMBEDDING_BACKFILL, 1800,
        "Embed any chunk lacking a semantic vector, so the corpus backfills "
        "itself automatically once an embedding provider is configured.",
    ),
    ScheduleSpec(
        # Daily. Scores change continuously through enrichment; this exists
        # for the decay that happens when nothing arrives at all.
        JobKind.QUALITY_REFRESH, 24 * 3600,
        "Recompute the Data Quality Score for every company, so freshness "
        "decay is reflected even when no new data has arrived.",
    ),
    ScheduleSpec(
        # Daily. The filing-triggered path is primary: a new document rescores
        # its company within the post-processing cycle. This sweep exists
        # because several modules read time-sensitive evidence — the
        # twelve-month news window rolls forward every day, and an
        # announcement ageing out of it changes the score with no new data
        # arriving. It is also cheap: an unchanged input fingerprint writes
        # nothing, so a quiet universe costs one scoring pass and no rows.
        JobKind.AI_SCORE_REFRESH, 24 * 3600,
        "Recalculate the ten-module AI score across the universe, recording "
        "a new permanent version only where the evidence has actually moved.",
    ),
    ScheduleSpec(
        # Daily. A company only gets selected while it lacks a usable financial
        # history, so once the universe is covered a pass is one cheap COUNT
        # and nothing to write. An interval short enough to fill a newly-added
        # company quickly, long enough not to hammer the source provider.
        JobKind.FINANCIALS_BACKFILL, 24 * 3600,
        "Ingest canonical annual financials for universe companies that "
        "still lack a usable history.",
    ),
    ScheduleSpec(
        JobKind.USAGE_ROLLUP, 900,
        "Aggregate raw usage events into the per-period counters.",
    ),
    ScheduleSpec(
        JobKind.BACKUP, 24 * 3600,
        "Write a consistent database snapshot to the backup location.",
    ),
    ScheduleSpec(
        JobKind.RETENTION_SWEEP, 24 * 3600,
        "Delete data older than each tenant's retention limit.",
    ),
)

