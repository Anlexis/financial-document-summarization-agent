"""AgentCore Platform v1.0 — FIN-C2-004 PreProcessNode"""

# Input boundary for the summarization pipeline. Two responsibilities:
#
#   1. Document screening — type check, empty check, length guard,
#      prompt-injection scan, PII detection (or the [MASKED] sentinel the
#      framework input gate substitutes for detected PII).
#   2. Caller-metadata validation — every field read from the caller-supplied
#      input_context channel is validated against explicit bounds before any
#      later node may consume it. Invalid values fail CLOSED (the request is
#      rejected, naming the field but never echoing the value); absent fields
#      degrade to documented defaults.
#
# Every code path emits an audit event.

import math
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.services.progress import emit_progress

# Prompt-injection detection patterns for the document text.
_INJECTION_PATTERNS = re.compile(
    r"(ignore previous instructions|disregard your prompt|you are now|system prompt|"
    r"<script|javascript:|DROP TABLE|UNION SELECT|exec\(|eval\()",
    re.IGNORECASE,
)

# PII detection patterns — also catches the [MASKED] sentinel the framework
# input gate substitutes for detected PII before execute() runs.
_PII_PATTERNS = re.compile(
    r"(\b\d{3}-\d{2}-\d{4}\b"  # SSN
    r"|\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"  # credit card
    r"|\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"  # email
    r"|\b\d{3}[\s.\-]?\d{3}[\s.\-]?\d{4}\b"  # phone
    r"|\[MASKED\])",  # already-masked sentinel
)

_MAX_DOC_CHARS = 50_000  # guard against extremely large documents

# ── Caller-metadata contract (input_context) ──────────────────────────────────
#
# The caller may pass structured document metadata alongside the text. Every
# consumed field is validated here; nothing outside this contract can influence
# the pipeline. Free text is not accepted on any metadata field — identifiers
# only — so caller metadata can never carry renderable prose into the report.
#
#   document_type          identifier [a-z0-9_]{1,32}     default "financial_document"
#   fiscal_period          identifier [a-z0-9_]{1,32}     default "" (omitted)
#   materiality_threshold  finite number in [0, 1e12]     default 0.0 (all amounts)
#   focus_terms            list of <= 10 identifiers      default []

_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_FOCUS_TERMS = 10
_THRESHOLD_MIN = 0.0
_THRESHOLD_MAX = 1e12

_DEFAULT_DOCUMENT_TYPE = "financial_document"


def _finite_in_range(value: Any, lo: float, hi: float) -> tuple[bool, float]:
    """Parse a caller-controlled numeric defensively; fail CLOSED.

    Accepts int/float only (bool is explicitly rejected — it is an int
    subclass). NaN and +/-Infinity parse fine as floats but every ordered
    comparison against them returns False, which would silently disable any
    bound built on such a comparison — so non-finite values are rejected
    outright, as are values outside [lo, hi].

    Returns (ok, parsed_value); parsed_value is 0.0 whenever ok is False.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, 0.0
    num = float(value)
    if not math.isfinite(num):
        return False, 0.0
    if num < lo or num > hi:
        return False, 0.0
    return True, num


def _reject(field: str, state: dict[str, Any]) -> dict[str, Any]:
    """Build the fail-closed rejection for an invalid caller-metadata field.

    Names the field only — the rejected value is never echoed into the
    error log or the response.
    """
    emit_trace_event(
        "pre_process_error",
        {"reason": "invalid_caller_metadata", "field": field},
        state,
    )
    emit_progress(INPUT_REJECTED)
    # A value the caller can correct: nothing downstream runs, and the
    # run COMPLETES carrying the reason so the request can be sent again.
    return {
        "status": AgentStatus.SUCCESS.value,
        "error_code": "INVALID_REQUEST",
        "error_log": [f"PreProcessNode: input_context field '{field}' failed validation"],
        "error_message": f"Invalid value for caller metadata field '{field}'.",
    }


class PreProcessNode(FunctionNode):
    """Validate and pre-screen the incoming financial document and metadata.

    Document gate: type check -> empty check -> length guard ->
    injection scan -> PII detection (or [MASKED] sentinel check).
    Metadata gate: every consumed input_context field validated against
    explicit bounds (identifiers inert, numerics finite and bounded).
    Emits an audit event on EVERY code path.
    """

    # External entry gate — callers must be at least VERIFIED_EXTERNAL.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})
        if not isinstance(input_context, dict):
            return _reject("input_context", state)

        # ── Type guard ────────────────────────────────────────────────────
        if not isinstance(user_input, str):
            emit_trace_event(
                "pre_process_error",
                {"reason": "invalid_type", "type": type(user_input).__name__},
                state,
            )
            emit_progress(INVALID_VALUE)
            # A value the caller can correct: nothing downstream runs, and the
            # run COMPLETES carrying the reason so the request can be sent again.
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: user_input must be a string"],
                "error_message": "Invalid input type.",
            }

        # ── Empty guard ───────────────────────────────────────────────────
        stripped = user_input.strip()
        if not stripped:
            emit_trace_event(
                "pre_process_error",
                {"reason": "empty_input"},
                state,
            )
            emit_progress(EMPTY_INPUT)
            # A value the caller can correct: nothing downstream runs, and the
            # run COMPLETES carrying the reason so the request can be sent again.
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty"],
                "error_message": "Input document must not be empty.",
            }

        # ── Length guard ──────────────────────────────────────────────────
        if len(stripped) > _MAX_DOC_CHARS:
            emit_trace_event(
                "pre_process_error",
                {"reason": "oversized_input", "length": len(stripped)},
                state,
            )
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [
                    f"PreProcessNode: document exceeds {_MAX_DOC_CHARS} char limit " f"(got {len(stripped)})"
                ],
                "error_message": "Document too large; split and resubmit.",
            }

        # ── Injection scan ────────────────────────────────────────────────
        # The framework input gate pre-masks PII to [MASKED] before execute()
        # runs. The injection scan operates on whatever we receive (masked or
        # raw) since prompt-injection content is not affected by PII masking.
        if _INJECTION_PATTERNS.search(stripped):
            emit_trace_event(
                "pre_process_blocked",
                {"reason": "injection_detected"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: prompt injection detected"],
                "error_message": "Input blocked: potential prompt injection.",
            }

        # ── PII detection ─────────────────────────────────────────────────
        # Primary path: the framework input gate pre-masks PII, so the
        # [MASKED] sentinel is checked. Fallback: raw patterns for direct
        # execute() callers (e.g. unit tests).
        if _PII_PATTERNS.search(stripped):
            emit_trace_event(
                "pre_process_blocked",
                {"reason": "pii_detected"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: PII detected in input"],
                "error_message": "Input contains PII; please redact and resubmit.",
            }

        # ── Caller-metadata validation (fail CLOSED per field) ────────────
        document_type = _DEFAULT_DOCUMENT_TYPE
        if "document_type" in input_context:
            raw_doc_type = input_context["document_type"]
            if not isinstance(raw_doc_type, str) or not _IDENTIFIER_RE.match(raw_doc_type):
                return _reject("document_type", state)
            document_type = raw_doc_type

        fiscal_period = ""
        if "fiscal_period" in input_context:
            raw_period = input_context["fiscal_period"]
            if not isinstance(raw_period, str) or not _IDENTIFIER_RE.match(raw_period):
                return _reject("fiscal_period", state)
            fiscal_period = raw_period

        materiality_threshold = 0.0
        if "materiality_threshold" in input_context:
            ok, materiality_threshold = _finite_in_range(
                input_context["materiality_threshold"], _THRESHOLD_MIN, _THRESHOLD_MAX
            )
            if not ok:
                return _reject("materiality_threshold", state)

        focus_terms: list[str] = []
        if "focus_terms" in input_context:
            raw_terms = input_context["focus_terms"]
            if not isinstance(raw_terms, list) or len(raw_terms) > _MAX_FOCUS_TERMS:
                return _reject("focus_terms", state)
            for term in raw_terms:
                if not isinstance(term, str) or not _IDENTIFIER_RE.match(term):
                    return _reject("focus_terms", state)
            focus_terms = list(raw_terms)

        # ── Success path ──────────────────────────────────────────────────
        result: dict[str, Any] = {
            "input_text": stripped,
            "validated_input": stripped,
            "document_type": document_type,
            "fiscal_period": fiscal_period,
            "materiality_threshold": materiality_threshold,
            "focus_terms": focus_terms,
            "status": AgentStatus.SUCCESS.value,
        }

        emit_trace_event(
            "pre_process_complete",
            {
                "input_length": len(stripped),
                "document_type": document_type,
                "focus_term_count": len(focus_terms),
            },
            state,
        )
        return result
