"""AgentCore Platform v1.0 — FIN-C2-004 SummarizeNode"""

# Produces a structured summary from the validated financial document:
#
#   executive summary — extractive: sentences are scored on financial-keyword
#       density, monetary content and document position; the top sentences are
#       rendered in document order;
#   key figures — monetary amounts and percentage figures are extracted from
#       the full document and reported as AGGREGATES only (count, total,
#       largest), filtered by the caller's materiality threshold and rendered
#       pre-rounded to the external 1,000-unit grid;
#   risk flags — a risk-category lexicon (plus the caller's validated focus
#       terms) is counted across the document;
#   action items — sentences carrying directive/commitment language.
#
# All output is computed from the validated document text and the validated
# caller metadata — absent metadata degrades to documented defaults.

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Single source of truth for the monetary-form grammar: the output gate's
# token recogniser. Extraction here and enforcement there must agree, or a
# figure could be aggregated under one definition and leak under another.
from src.nodes.chunk_node import chunk_text
from src.nodes.post_process_node import _EXTERNAL_ROUND_UNIT, _NUM_TOKEN_RE

_TOP_N_SENTENCES = 3  # sentences rendered into the executive summary
_MAX_SENTENCE_CHARS = 300
_MAX_ACTION_ITEMS = 3

_PERCENT_RE = re.compile(r"[+-]?\d{1,3}(?:\.\d+)?\s*%")

_FINANCIAL_KEYWORDS = (
    "revenue",
    "income",
    "profit",
    "loss",
    "margin",
    "earnings",
    "ebitda",
    "cash",
    "dividend",
    "cost",
    "expense",
    "capital",
    "debt",
    "equity",
    "guidance",
    "forecast",
    "growth",
    "quarter",
    "fiscal",
    "sales",
    "operating",
    "asset",
    "liability",
    "impairment",
)

_RISK_LEXICON: dict[str, tuple[str, ...]] = {
    "market risk": ("market risk", "market volatility", "volatility"),
    "credit risk": ("credit risk", "default", "impairment", "downgrade", "non-performing"),
    "liquidity risk": ("liquidity", "funding pressure", "cash shortfall"),
    "operational risk": ("operational risk", "supply-chain", "supply chain", "disruption", "outage"),
    "regulatory risk": ("regulatory", "compliance", "litigation", "legal proceedings"),
    "currency risk": ("currency risk", "foreign exchange", "exchange rate", "fx exposure"),
}

_ACTION_RE = re.compile(
    r"\b(approved?|will|must|should|plans? to|expects? to|review|monitor|update|submit|due by)\b",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    """Split the document into non-empty sentences."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _truncate(sentence: str) -> str:
    if len(sentence) <= _MAX_SENTENCE_CHARS:
        return sentence
    return sentence[: _MAX_SENTENCE_CHARS - 1].rstrip() + "…"


def _extract_amounts(text: str) -> list[float]:
    """Extract every monetary-form token's absolute value.

    Parsed as float, matching the output gate's own snap: the shared recogniser
    absorbs a decimal fraction into the token, so int() here would raise on any
    document that carries one — and, before it absorbed the fraction, it counted
    that fraction as a monetary amount all of its own and inflated the totals.
    """
    amounts: list[float] = []
    for match in _NUM_TOKEN_RE.finditer(text):
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        amounts.append(abs(float(token.replace(",", ""))))
    return amounts


def _grid(value: float) -> str:
    """Render a value pre-rounded onto the external 1,000-unit grid."""
    snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
    return f"{int(snapped):,d}"


def _score_sentence(index: int, sentence: str) -> int:
    lowered = sentence.lower()
    score = sum(1 for kw in _FINANCIAL_KEYWORDS if kw in lowered)
    if _NUM_TOKEN_RE.search(sentence):
        score += 2
    if _PERCENT_RE.search(sentence):
        score += 1
    score += max(0, _TOP_N_SENTENCES - index)  # lead sentences carry context
    return score


def _executive_summary(sentences: list[str]) -> str:
    ranked = sorted(
        range(len(sentences)),
        key=lambda i: (-_score_sentence(i, sentences[i]), i),
    )
    chosen = sorted(ranked[:_TOP_N_SENTENCES])
    return " ".join(_truncate(sentences[i]) for i in chosen)


def _key_figures(text: str, materiality_threshold: float) -> str:
    amounts = _extract_amounts(text)
    material = [a for a in amounts if a >= materiality_threshold]
    percents = _PERCENT_RE.findall(text)

    lines: list[str] = []
    if not amounts:
        lines.append("No monetary amounts detected in the document.")
    else:
        lines.append(
            f"Monetary amounts detected: {len(amounts)} " f"({len(material)} at or above the materiality threshold)."
        )
        if material:
            lines.append(
                f"Material amounts (absolute, rounded): total ≈ {_grid(sum(material))}; "
                f"largest ≈ {_grid(max(material))}."
            )
    if percents:
        lines.append(f"Percentage figures detected: {len(percents)}.")
    return "\n".join(lines)


# One alternation per category, longest phrase first: each text span is
# counted once even when phrases overlap ("market volatility" is one mention,
# not a "market volatility" hit plus a "volatility" hit).
_RISK_CATEGORY_RES = {
    category: re.compile(r"\b(?:" + "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True)) + r")\b")
    for category, phrases in _RISK_LEXICON.items()
}


def _focus_term_re(term: str) -> re.Pattern[str]:
    """Focus terms are validated identifiers; underscores match any single
    space or hyphen separator ("supply_chain" matches "supply chain" and
    "supply-chain")."""
    return re.compile(r"\b" + r"[\s\-]".join(re.escape(part) for part in term.split("_")) + r"\b")


def _risk_flags(text: str, focus_terms: list[str]) -> str:
    lowered = text.lower()
    findings: list[str] = []
    for category, category_re in _RISK_CATEGORY_RES.items():
        mentions = len(category_re.findall(lowered))
        if mentions:
            findings.append(f"{category} ({mentions} mention{'s' if mentions != 1 else ''})")
    for term in focus_terms:
        mentions = len(_focus_term_re(term).findall(lowered))
        findings.append(f"focus term '{term}' ({mentions} mention{'s' if mentions != 1 else ''})")
    if not findings:
        return "No risk-related terms detected."
    return "; ".join(findings) + "."


def _action_items(sentences: list[str]) -> str:
    items = [s for s in sentences if _ACTION_RE.search(s)][:_MAX_ACTION_ITEMS]
    if not items:
        return "No explicit action items detected."
    return "\n".join(f"- {_truncate(s)}" for s in items)


class SummarizeNode(FunctionNode):
    """Compute the structured summary from the validated document.

    Reads: validated_input (str); chunked_segments when a caller pre-chunked
           the document (joined back for analysis); document metadata written
           by PreProcessNode (materiality_threshold, focus_terms).
    Writes: summary_output, key_figures, risk_flags, action_items,
            chunked_segments
    Emits a summarize_complete audit event.
    """

    # Inner processing node — no external caller gating required
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        text = state.get("validated_input", state.get("input_text", "")) or ""
        segments: list[str] = state.get("chunked_segments", []) or []
        if not text and segments:
            text = " ".join(segments)

        if not isinstance(text, str) or not text:
            emit_trace_event(
                "summarize_error",
                {"reason": "no_input_available"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["SummarizeNode: neither validated_input nor chunked_segments found"],
            }

        if not segments:
            segments = chunk_text(text)

        materiality_threshold = state.get("materiality_threshold", 0.0)
        if not isinstance(materiality_threshold, (int, float)) or isinstance(materiality_threshold, bool):
            materiality_threshold = 0.0
        raw_focus_terms = state.get("focus_terms", [])
        focus_terms = [t for t in raw_focus_terms if isinstance(t, str)] if isinstance(raw_focus_terms, list) else []

        sentences = _split_sentences(text)
        summary = _executive_summary(sentences) if sentences else _truncate(text)

        result: dict[str, Any] = {
            "chunked_segments": segments,
            "summary_output": summary,
            "key_figures": _key_figures(text, float(materiality_threshold)),
            "risk_flags": _risk_flags(text, focus_terms),
            "action_items": _action_items(sentences),
            "status": AgentStatus.SUCCESS.value,
        }

        emit_trace_event(
            "summarize_complete",
            {
                "segment_count": len(segments),
                "sentence_count": len(sentences),
                "summary_length": len(summary),
            },
            state,
        )
        return result
