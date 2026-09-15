"""Internal Question Planner — Part 2B.

    User Question
      -> Internal Language Understanding      (Language Adapter, inbound)
      -> Internal Question Planner            <- this package
      -> Evidence / data requirements
      -> Existing deterministic engines       (Part 2C)
      -> Canonical English answer
      -> Audit + guardrails
      -> Internal Language Renderer           (Part 2A)
      -> Display

**Shadow mode.** Nothing in the production answer path calls this package
yet. ``ResearchAnalyst`` continues to route through
``FinancialIntentResolver`` and the two deterministic engines exactly as it
did before this package existed. Wiring the plan into execution is Part 2C.

What the planner does:

* recognises which of the platform's existing intents a question asks for,
  in the order asked, across English, Hindi and Hinglish;
* identifies the company the question is about, through
  ``CompanyService.named_in`` — never by inventing one;
* states what evidence answering it would require, naming only citation
  keys the ContextBuilder actually publishes;
* says which route execution would take, without taking it.

What it never does: answer, score, value, retrieve, embed, translate for
output, write to the database, or call an external provider. The package's
own test suite enforces each of those by static inspection.

The public surface is deliberately small::

    from app.services.ai.planner import QuestionPlanner
    plan = QuestionPlanner(company_resolver=svc.named_in).plan(question)
"""
from __future__ import annotations

from .evidence import REQUIRED_EVIDENCE, evidence_for, evidence_for_all
from .intent_matcher import IntentMatcher
from .question_planner import (
    CompanyLike, CompanyResolver, QuestionPlanner,
)
from .types import (
    Confidence, EntityResolution, EntityStatus, EvidenceRequirement,
    ExecutionRoute, IntentFamily, IntentMatch, QueryType, QuestionPlan,
)
from .vocabulary import INTENT_VOCABULARY, IntentSpec

__all__ = [
    "Confidence", "CompanyLike", "CompanyResolver", "EntityResolution",
    "EntityStatus", "EvidenceRequirement", "ExecutionRoute", "INTENT_VOCABULARY",
    "IntentFamily", "IntentMatch", "IntentMatcher", "IntentSpec", "QueryType",
    "QuestionPlan", "QuestionPlanner", "REQUIRED_EVIDENCE", "evidence_for",
    "evidence_for_all",
]
