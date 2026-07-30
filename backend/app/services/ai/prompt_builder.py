"""Prompt assembly.

Composes the final message list from four sources: the shared guardrail
preamble, the capability's own template, the grounded evidence block, and the
conversation memory.

Ordering is deliberate. The preamble comes first so its rules frame everything
that follows; evidence comes before the task so the model reads the permitted
figures before it knows what it is being asked to argue; memory comes last so
recency works in the analyst's favour.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.ai.types import Citation, CompletionRequest, Message, Role
from app.services.ai.context_builder import GroundedContext
from app.services.ai.memory import ConversationMemory
from app.services.ai.prompt_library import (
    SYSTEM_PREAMBLE, OutputStyle, PromptTemplate,
)

STYLE_INSTRUCTIONS = {
    OutputStyle.MARKDOWN:
        "Format as markdown with short paragraphs and bold sub-headings.",
    OutputStyle.EXECUTIVE_SUMMARY:
        "Format as a tight executive summary: one opening judgement, then three "
        "to five bullets. No more than 200 words.",
    OutputStyle.BOARD_PRESENTATION:
        "Format for a board pack: numbered sections, one claim per line, no "
        "narrative padding. Lead with the conclusion.",
    OutputStyle.REPORT_SECTION:
        "Format as a research-report section with a heading and flowing prose "
        "suitable for a PDF.",
}


@dataclass(frozen=True, slots=True)
class BuiltPrompt:
    """The assembled request plus the evidence it is allowed to cite."""

    request: CompletionRequest
    citations: list[Citation]
    prompt_key: str
    prompt_version: int
    approx_prompt_tokens: int


class PromptBuilder:
    def __init__(self, *, temperature: float | None = None,
                 max_tokens: int | None = None) -> None:
        self.temperature = temperature
        self.max_tokens = max_tokens

    def build(
        self,
        template: PromptTemplate,
        context: GroundedContext,
        *,
        question: str = "",
        memory: ConversationMemory | None = None,
        style: OutputStyle | None = None,
        include_history: bool = True,
        extra: str = "",
    ) -> BuiltPrompt:
        """Assemble the full request."""
        # Only the evidence families this capability declared, so a moat prompt
        # is not diluted with sixty unrelated figures.
        kinds = list(template.evidence) or None
        evidence_block = context.render_evidence(kinds)
        selected = [
            c for c in context.citations if kinds is None or c.kind in kinds
        ]

        header = (
            f"COMPANY: {context.name} ({context.ticker})"
            + (f" — {context.sector}" if context.sector else "")
        )
        chosen_style = style or template.style
        style_note = STYLE_INSTRUCTIONS.get(chosen_style, "")

        body = template.render(
            evidence_block=evidence_block,
            gaps=context.render_gaps(),
            question=f"ANALYST QUESTION: {question}\n" if question else "",
            extra=extra,
        )

        messages: list[Message] = [
            Message(Role.SYSTEM, SYSTEM_PREAMBLE),
            Message(Role.SYSTEM, header),
        ]
        if memory and memory.state_summary():
            messages.append(Message(Role.SYSTEM, f"SESSION: {memory.state_summary()}"))
        if include_history and memory:
            messages.extend(memory.recent())

        messages.append(Message(Role.USER, f"{body}\n\n{style_note}".strip()))

        approx = sum(len(m.content) for m in messages) // 4
        return BuiltPrompt(
            request=CompletionRequest(
                messages=messages,
                temperature=self.temperature if self.temperature is not None
                else template.temperature,
                max_tokens=self.max_tokens or template.max_tokens,
            ),
            citations=selected,
            prompt_key=template.key,
            prompt_version=template.version,
            approx_prompt_tokens=approx,
        )
