"""Conversation memory.

Holds what the analyst is working on: the company under discussion, the
valuation assumptions in play, documents attached, and the recent exchange.

Two design points:

* **Context is pinned, not inferred.** The company is set explicitly rather
  than re-derived from each message, so "what about its debt?" resolves
  correctly on the fifth turn.
* **History is trimmed by tokens, not turns.** A conversation with three long
  analyses costs more context than twenty short questions, and trimming by
  message count would silently blow the window.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.domain.ai.types import Message, Role

#: Approximate characters per token, used for trimming.
CHARS_PER_TOKEN = 4
#: Budget reserved for conversation history, leaving room for evidence.
HISTORY_TOKEN_BUDGET = 1500
MAX_TURNS = 40


@dataclass(slots=True)
class Turn:
    role: Role
    content: str
    at: float = field(default_factory=time.time)
    citations: list[str] = field(default_factory=list)

    @property
    def approx_tokens(self) -> int:
        return max(1, len(self.content) // CHARS_PER_TOKEN)


@dataclass(slots=True)
class ConversationMemory:
    """One analyst session."""

    session_id: str
    #: The company under discussion. Pinned, so follow-ups resolve.
    company_id: str | None = None
    ticker: str | None = None
    company_name: str | None = None
    #: Valuation assumptions the user has chosen in this session.
    assumptions: dict[str, float] = field(default_factory=dict)
    #: Weight profile and horizon selected.
    preferences: dict[str, str] = field(default_factory=dict)
    #: Documents attached to the conversation (Module 7 will populate).
    documents: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------- mutation
    def set_company(self, company_id: str, ticker: str, name: str) -> None:
        if self.company_id != company_id:
            # Switching company invalidates assumptions tied to the old one.
            self.assumptions.clear()
        self.company_id, self.ticker, self.company_name = company_id, ticker, name

    def remember_assumption(self, key: str, value: float) -> None:
        self.assumptions[key] = value

    def attach_document(self, name: str) -> None:
        if name not in self.documents:
            self.documents.append(name)

    def add(self, role: Role, content: str, citations: list[str] | None = None) -> None:
        self.turns.append(Turn(role, content, citations=citations or []))
        if len(self.turns) > MAX_TURNS:
            self.turns = self.turns[-MAX_TURNS:]

    def clear(self) -> None:
        self.turns.clear()

    # -------------------------------------------------------------- reading
    def recent(self, budget: int = HISTORY_TOKEN_BUDGET) -> list[Message]:
        """Most recent turns that fit the token budget, oldest first."""
        selected: list[Turn] = []
        used = 0
        for turn in reversed(self.turns):
            cost = turn.approx_tokens
            if used + cost > budget and selected:
                break
            selected.append(turn)
            used += cost
        return [Message(t.role, t.content) for t in reversed(selected)]

    def state_summary(self) -> str:
        """A line describing pinned state, injected into the prompt."""
        parts: list[str] = []
        if self.company_name:
            parts.append(f"Company under discussion: {self.company_name} ({self.ticker}).")
        if self.assumptions:
            values = ", ".join(f"{k}={v}" for k, v in self.assumptions.items())
            parts.append(f"Assumptions the analyst has set: {values}.")
        if self.preferences:
            values = ", ".join(f"{k}={v}" for k, v in self.preferences.items())
            parts.append(f"Preferences: {values}.")
        if self.documents:
            parts.append(f"Documents attached: {', '.join(self.documents)}.")
        return " ".join(parts)

    @property
    def turn_count(self) -> int:
        return len(self.turns)


class MemoryStore:
    """In-process session store.

    Deliberately simple. Sessions are short-lived working state, not durable
    records; the durable artefact is the saved report. Swapping this for Redis
    is a one-class change if sessions ever need to span processes.
    """

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self._sessions: dict[str, ConversationMemory] = {}

    def get(self, session_id: str) -> ConversationMemory:
        memory = self._sessions.get(session_id)
        if memory is None:
            if len(self._sessions) >= self.capacity:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].created_at)
                self._sessions.pop(oldest, None)
            memory = ConversationMemory(session_id=session_id)
            self._sessions[session_id] = memory
        return memory

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def all_sessions(self) -> list[ConversationMemory]:
        return list(self._sessions.values())


#: Process-wide store shared by the API layer.
memory_store = MemoryStore()
