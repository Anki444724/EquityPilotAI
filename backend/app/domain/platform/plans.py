"""Plans, feature flags, quotas and the entitlement decision.

The commercial shape of the product lives here, and only here. A plan is a
named bundle of three things:

* **features** — capabilities that are either present or absent;
* **quotas** — metered allowances that reset on a period;
* **limits** — hard ceilings that do not reset (seats, storage, retention).

Two rules make this maintainable.

**Entitlement is one function.** `evaluate()` answers "may this tenant do this
now?" and returns *why not* when the answer is no. Every enforcement point —
API dependency, background worker, admin panel preview — calls the same
function, so there is exactly one place where the commercial policy is
expressed and exactly one behaviour to test.

**Features are derived from the product, not invented.** Each `Feature` maps
to a capability Modules 1-9 actually ship. A flag that gates nothing is worse
than no flag: it lets the pricing page promise something the code cannot
withhold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
class Feature(StrEnum):
    """Gateable capabilities. Each corresponds to shipped functionality."""

    # research
    HISTORICAL_FINANCIALS = "historical_financials"    # Module 2
    FORECAST_ENGINE = "forecast_engine"                # Module 3
    SCENARIO_ANALYSIS = "scenario_analysis"            # Module 3 bull/bear
    VALUATION_ENGINE = "valuation_engine"              # Module 4
    SENSITIVITY_ANALYSIS = "sensitivity_analysis"      # Module 4
    SCORING_ENGINE = "scoring_engine"                  # Module 5
    CUSTOM_WEIGHTS = "custom_weights"                  # Module 5 profiles

    # AI
    AI_ANALYST = "ai_analyst"                          # Module 6
    AI_CHAT = "ai_chat"                                # Module 6 chat capability

    # documents
    DOCUMENT_UPLOAD = "document_upload"                # Module 7
    DOCUMENT_OCR = "document_ocr"                      # Module 7 scanned path
    SEMANTIC_SEARCH = "semantic_search"                # Module 7 embeddings

    # portfolio
    PORTFOLIO_TRACKING = "portfolio_tracking"          # Module 8
    PORTFOLIO_ANALYTICS = "portfolio_analytics"        # Module 8 risk/attribution
    ALERTS = "alerts"                                  # Module 8

    # reports
    REPORT_GENERATION = "report_generation"            # Module 9
    REPORT_PDF = "report_pdf"
    REPORT_EXCEL = "report_excel"
    REPORT_WORD = "report_word"
    WHITE_LABEL = "white_label"                        # tenant branding on reports

    # platform
    API_ACCESS = "api_access"                          # programmatic API keys
    SSO = "sso"                                        # Google/GitHub for the tenant
    AUDIT_LOG = "audit_log"
    PRIORITY_SUPPORT = "priority_support"


FEATURE_LABELS: dict[Feature, str] = {
    Feature.HISTORICAL_FINANCIALS: "Historical financials & ratios",
    Feature.FORECAST_ENGINE: "Forecast engine",
    Feature.SCENARIO_ANALYSIS: "Bull / base / bear scenarios",
    Feature.VALUATION_ENGINE: "Valuation engine (10 methods)",
    Feature.SENSITIVITY_ANALYSIS: "Sensitivity & scenario grids",
    Feature.SCORING_ENGINE: "Institutional scoring",
    Feature.CUSTOM_WEIGHTS: "Custom scoring weight profiles",
    Feature.AI_ANALYST: "AI research analyst",
    Feature.AI_CHAT: "AI chat",
    Feature.DOCUMENT_UPLOAD: "Document upload & extraction",
    Feature.DOCUMENT_OCR: "OCR for scanned filings",
    Feature.SEMANTIC_SEARCH: "Semantic document search",
    Feature.PORTFOLIO_TRACKING: "Portfolio tracking",
    Feature.PORTFOLIO_ANALYTICS: "Attribution & risk analytics",
    Feature.ALERTS: "Live alerts",
    Feature.REPORT_GENERATION: "Research report generator",
    Feature.REPORT_PDF: "PDF export",
    Feature.REPORT_EXCEL: "Excel export",
    Feature.REPORT_WORD: "Word export",
    Feature.WHITE_LABEL: "White-label branding",
    Feature.API_ACCESS: "REST API access",
    Feature.SSO: "Google / GitHub single sign-on",
    Feature.AUDIT_LOG: "Audit trail",
    Feature.PRIORITY_SUPPORT: "Priority support",
}


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------
class Quota(StrEnum):
    """Metered allowances. Consumption is recorded per tenant per period."""

    AI_CALLS = "ai_calls"
    AI_TOKENS = "ai_tokens"
    REPORTS_GENERATED = "reports_generated"
    DOCUMENTS_PROCESSED = "documents_processed"
    DOCUMENT_PAGES = "document_pages"
    API_REQUESTS = "api_requests"
    EXPORTS = "exports"


QUOTA_LABELS: dict[Quota, str] = {
    Quota.AI_CALLS: "AI calls",
    Quota.AI_TOKENS: "AI tokens",
    Quota.REPORTS_GENERATED: "Reports generated",
    Quota.DOCUMENTS_PROCESSED: "Documents processed",
    Quota.DOCUMENT_PAGES: "Document pages",
    Quota.API_REQUESTS: "API requests",
    Quota.EXPORTS: "Exports",
}

QUOTA_UNITS: dict[Quota, str] = {
    Quota.AI_CALLS: "calls",
    Quota.AI_TOKENS: "tokens",
    Quota.REPORTS_GENERATED: "reports",
    Quota.DOCUMENTS_PROCESSED: "documents",
    Quota.DOCUMENT_PAGES: "pages",
    Quota.API_REQUESTS: "requests",
    Quota.EXPORTS: "exports",
}


class Limit(StrEnum):
    """Ceilings that do not reset with the billing period."""

    SEATS = "seats"
    PORTFOLIOS = "portfolios"
    WATCHLISTS = "watchlists"
    API_KEYS = "api_keys"
    STORAGE_MB = "storage_mb"
    RETENTION_DAYS = "retention_days"
    RATE_LIMIT_PER_MINUTE = "rate_limit_per_minute"


LIMIT_LABELS: dict[Limit, str] = {
    Limit.SEATS: "Seats",
    Limit.PORTFOLIOS: "Portfolios",
    Limit.WATCHLISTS: "Watchlists",
    Limit.API_KEYS: "API keys",
    Limit.STORAGE_MB: "Storage",
    Limit.RETENTION_DAYS: "Data retention",
    Limit.RATE_LIMIT_PER_MINUTE: "Rate limit",
}

#: Sentinel for "no ceiling". Chosen over `None` so arithmetic and comparison
#: work everywhere without a null check, and over `math.inf` so the value
#: survives a JSON round trip as an integer.
UNLIMITED = -1


def is_unlimited(value: int) -> bool:
    return value == UNLIMITED


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
class PlanTier(StrEnum):
    FREE = "free"
    BASIC = "basic"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class BillingPeriod(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


@dataclass(frozen=True, slots=True)
class PlanSpec:
    """The canonical definition of a plan.

    Seeded into the database so an operator can adjust pricing without a
    deploy, but the code default lives here so a fresh install is coherent and
    so the tests have a fixed point to assert against.
    """

    tier: PlanTier
    name: str
    tagline: str
    price_monthly_inr: int
    price_annual_inr: int
    features: frozenset[Feature]
    quotas: dict[Quota, int]
    limits: dict[Limit, int]
    trial_days: int = 0
    is_public: bool = True

    def has(self, feature: Feature) -> bool:
        return feature in self.features

    def quota(self, quota: Quota) -> int:
        """Allowance per period. Missing means zero, never unlimited — a quota
        nobody remembered to price must not be free."""
        return self.quotas.get(quota, 0)

    def limit(self, limit: Limit) -> int:
        return self.limits.get(limit, 0)

    @property
    def annual_discount_pct(self) -> float:
        """Saving from paying annually, as a fraction."""
        full = self.price_monthly_inr * 12
        if full <= 0:
            return 0.0
        return round(1 - (self.price_annual_inr / full), 4)


# --- feature bundles -------------------------------------------------------
F = Feature

_FREE_FEATURES = frozenset({
    F.HISTORICAL_FINANCIALS,
})

_BASIC_FEATURES = _FREE_FEATURES | {
    F.FORECAST_ENGINE, F.VALUATION_ENGINE, F.SCORING_ENGINE,
    F.DOCUMENT_UPLOAD, F.PORTFOLIO_TRACKING,
    F.REPORT_GENERATION, F.REPORT_PDF,
}

_PRO_FEATURES = _BASIC_FEATURES | {
    F.SCENARIO_ANALYSIS, F.SENSITIVITY_ANALYSIS, F.CUSTOM_WEIGHTS,
    F.AI_ANALYST, F.AI_CHAT,
    F.DOCUMENT_OCR, F.SEMANTIC_SEARCH,
    F.PORTFOLIO_ANALYTICS, F.ALERTS,
    F.REPORT_EXCEL, F.REPORT_WORD,
    F.API_ACCESS, F.AUDIT_LOG,
}

_ENTERPRISE_FEATURES = _PRO_FEATURES | {
    F.WHITE_LABEL, F.SSO, F.PRIORITY_SUPPORT,
}

Q = Quota
L = Limit

#: The four plans named in the brief. Prices are illustrative INR figures for
#: an Indian institutional product; they are seeded, editable data.
PLAN_CATALOGUE: dict[PlanTier, PlanSpec] = {
    PlanTier.FREE: PlanSpec(
        tier=PlanTier.FREE,
        name="Free",
        tagline="Explore the coverage universe and ten years of financials.",
        price_monthly_inr=0,
        price_annual_inr=0,
        features=_FREE_FEATURES,
        quotas={
            Q.AI_CALLS: 0, Q.AI_TOKENS: 0,
            Q.REPORTS_GENERATED: 0,
            Q.DOCUMENTS_PROCESSED: 0, Q.DOCUMENT_PAGES: 0,
            Q.API_REQUESTS: 1_000, Q.EXPORTS: 3,
        },
        limits={
            L.SEATS: 1, L.PORTFOLIOS: 0, L.WATCHLISTS: 1, L.API_KEYS: 0,
            L.STORAGE_MB: 0, L.RETENTION_DAYS: 30,
            L.RATE_LIMIT_PER_MINUTE: 30,
        },
    ),
    PlanTier.BASIC: PlanSpec(
        tier=PlanTier.BASIC,
        name="Basic",
        tagline="Forecast, value and score a focused watchlist.",
        price_monthly_inr=2_499,
        price_annual_inr=24_990,
        features=_BASIC_FEATURES,
        quotas={
            Q.AI_CALLS: 0, Q.AI_TOKENS: 0,
            Q.REPORTS_GENERATED: 20,
            Q.DOCUMENTS_PROCESSED: 25, Q.DOCUMENT_PAGES: 750,
            Q.API_REQUESTS: 25_000, Q.EXPORTS: 50,
        },
        limits={
            L.SEATS: 3, L.PORTFOLIOS: 2, L.WATCHLISTS: 5, L.API_KEYS: 0,
            L.STORAGE_MB: 512, L.RETENTION_DAYS: 365,
            L.RATE_LIMIT_PER_MINUTE: 120,
        },
        trial_days=14,
    ),
    PlanTier.PROFESSIONAL: PlanSpec(
        tier=PlanTier.PROFESSIONAL,
        name="Professional",
        tagline="The full research desk: AI analyst, documents, attribution.",
        price_monthly_inr=8_999,
        price_annual_inr=89_990,
        features=_PRO_FEATURES,
        quotas={
            Q.AI_CALLS: 2_000, Q.AI_TOKENS: 5_000_000,
            Q.REPORTS_GENERATED: 200,
            Q.DOCUMENTS_PROCESSED: 500, Q.DOCUMENT_PAGES: 20_000,
            Q.API_REQUESTS: 250_000, Q.EXPORTS: 1_000,
        },
        limits={
            L.SEATS: 15, L.PORTFOLIOS: 25, L.WATCHLISTS: 50, L.API_KEYS: 10,
            L.STORAGE_MB: 20_480, L.RETENTION_DAYS: 1_095,
            L.RATE_LIMIT_PER_MINUTE: 600,
        },
        trial_days=14,
    ),
    PlanTier.ENTERPRISE: PlanSpec(
        tier=PlanTier.ENTERPRISE,
        name="Enterprise",
        tagline="Unmetered research for the whole desk, with SSO and branding.",
        price_monthly_inr=34_999,
        price_annual_inr=349_990,
        features=_ENTERPRISE_FEATURES,
        quotas={
            Q.AI_CALLS: UNLIMITED, Q.AI_TOKENS: UNLIMITED,
            Q.REPORTS_GENERATED: UNLIMITED,
            Q.DOCUMENTS_PROCESSED: UNLIMITED, Q.DOCUMENT_PAGES: UNLIMITED,
            Q.API_REQUESTS: UNLIMITED, Q.EXPORTS: UNLIMITED,
        },
        limits={
            L.SEATS: UNLIMITED, L.PORTFOLIOS: UNLIMITED,
            L.WATCHLISTS: UNLIMITED, L.API_KEYS: 50,
            L.STORAGE_MB: UNLIMITED, L.RETENTION_DAYS: UNLIMITED,
            L.RATE_LIMIT_PER_MINUTE: 2_400,
        },
        trial_days=30,
    ),
}

#: Ascending order of capability. Used for upgrade paths and the pricing grid.
PLAN_ORDER: tuple[PlanTier, ...] = (
    PlanTier.FREE, PlanTier.BASIC, PlanTier.PROFESSIONAL, PlanTier.ENTERPRISE,
)


def plan(tier: PlanTier) -> PlanSpec:
    return PLAN_CATALOGUE[tier]


def upgrade_path(tier: PlanTier) -> tuple[PlanTier, ...]:
    """Tiers strictly above `tier`, cheapest first."""
    return PLAN_ORDER[PLAN_ORDER.index(tier) + 1:]


def cheapest_plan_with(feature: Feature) -> PlanTier | None:
    """The lowest tier that includes `feature` — what the upgrade prompt
    should offer. Returns None if no plan sells it."""
    for tier in PLAN_ORDER:
        if PLAN_CATALOGUE[tier].has(feature):
            return tier
    return None


# ---------------------------------------------------------------------------
# Subscription state
# ---------------------------------------------------------------------------
class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


#: Statuses under which the plan's entitlements still apply. A cancelled
#: subscription keeps working until the period ends, which is why CANCELLED is
#: absent here and handled by the period-end date instead.
ENTITLED_STATUSES: frozenset[SubscriptionStatus] = frozenset({
    SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE,
})


# ---------------------------------------------------------------------------
# The entitlement decision
# ---------------------------------------------------------------------------
class DenialReason(StrEnum):
    """Why an action was refused. Distinct reasons because the product should
    say "upgrade to Professional" and "you have used 200 of 200 reports this
    month" differently — they call for different user actions."""

    ALLOWED = "allowed"
    FEATURE_NOT_IN_PLAN = "feature_not_in_plan"
    QUOTA_EXCEEDED = "quota_exceeded"
    LIMIT_REACHED = "limit_reached"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"
    TENANT_SUSPENDED = "tenant_suspended"
    TENANT_READ_ONLY = "tenant_read_only"


@dataclass(frozen=True, slots=True)
class Entitlement:
    """The answer, with enough context for the UI to explain it."""

    allowed: bool
    reason: DenialReason = DenialReason.ALLOWED
    message: str = ""
    feature: Feature | None = None
    quota: Quota | None = None
    limit: Limit | None = None
    used: int | None = None
    allowance: int | None = None
    upgrade_to: PlanTier | None = None

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Entitlement(allowed=True)


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """Consumption of one quota within the current period."""

    quota: Quota
    used: int
    allowance: int

    @property
    def unlimited(self) -> bool:
        return is_unlimited(self.allowance)

    @property
    def remaining(self) -> int:
        if self.unlimited:
            return UNLIMITED
        return max(0, self.allowance - self.used)

    @property
    def utilisation(self) -> float:
        """Fraction of the allowance consumed, 0-1+. Unlimited reports 0.0 so
        a progress bar renders empty rather than undefined."""
        if self.unlimited or self.allowance <= 0:
            return 0.0
        return round(self.used / self.allowance, 4)

    @property
    def exhausted(self) -> bool:
        if self.unlimited:
            return False
        return self.used >= self.allowance


def evaluate(
    *,
    spec: PlanSpec,
    subscription_status: SubscriptionStatus,
    tenant_blocked: bool = False,
    tenant_read_only: bool = False,
    feature: Feature | None = None,
    quota: Quota | None = None,
    quota_used: int = 0,
    quota_requested: int = 1,
    limit: Limit | None = None,
    limit_used: int = 0,
    limit_requested: int = 1,
) -> Entitlement:
    """The single entitlement decision for the whole platform.

    Checks run cheapest-and-most-fundamental first, so the message a user sees
    names the real obstacle: a suspended tenant is told it is suspended rather
    than that it is out of report credits.
    """
    if tenant_blocked:
        return Entitlement(
            allowed=False, reason=DenialReason.TENANT_SUSPENDED,
            message="This organisation is suspended. Contact your administrator.",
        )

    if subscription_status not in ENTITLED_STATUSES:
        return Entitlement(
            allowed=False, reason=DenialReason.SUBSCRIPTION_INACTIVE,
            message=(
                f"Subscription is {subscription_status.value.replace('_', ' ')}. "
                "Renew to restore access."
            ),
        )

    if tenant_read_only:
        return Entitlement(
            allowed=False, reason=DenialReason.TENANT_READ_ONLY,
            message=(
                "Billing is past due — the workspace is read-only until "
                "payment succeeds."
            ),
        )

    if feature is not None and not spec.has(feature):
        target = cheapest_plan_with(feature)
        return Entitlement(
            allowed=False, reason=DenialReason.FEATURE_NOT_IN_PLAN,
            message=(
                f"{FEATURE_LABELS[feature]} is not included in the "
                f"{spec.name} plan."
            ),
            feature=feature, upgrade_to=target,
        )

    if limit is not None:
        allowance = spec.limit(limit)
        if not is_unlimited(allowance) and limit_used + limit_requested > allowance:
            return Entitlement(
                allowed=False, reason=DenialReason.LIMIT_REACHED,
                message=(
                    f"{LIMIT_LABELS[limit]} limit reached "
                    f"({limit_used} of {allowance}) on the {spec.name} plan."
                ),
                limit=limit, used=limit_used, allowance=allowance,
                upgrade_to=_next_tier_raising_limit(spec.tier, limit, allowance),
            )

    if quota is not None:
        allowance = spec.quota(quota)
        if not is_unlimited(allowance) and quota_used + quota_requested > allowance:
            return Entitlement(
                allowed=False, reason=DenialReason.QUOTA_EXCEEDED,
                message=(
                    f"{QUOTA_LABELS[quota]} quota exhausted "
                    f"({quota_used} of {allowance} this period) on the "
                    f"{spec.name} plan."
                ),
                quota=quota, used=quota_used, allowance=allowance,
                upgrade_to=_next_tier_raising_quota(spec.tier, quota, allowance),
            )

    return ALLOWED


def _next_tier_raising_quota(tier: PlanTier, quota: Quota, current: int) -> PlanTier | None:
    for candidate in upgrade_path(tier):
        allowance = PLAN_CATALOGUE[candidate].quota(quota)
        if is_unlimited(allowance) or allowance > current:
            return candidate
    return None


def _next_tier_raising_limit(tier: PlanTier, limit: Limit, current: int) -> PlanTier | None:
    for candidate in upgrade_path(tier):
        allowance = PLAN_CATALOGUE[candidate].limit(limit)
        if is_unlimited(allowance) or allowance > current:
            return candidate
    return None
