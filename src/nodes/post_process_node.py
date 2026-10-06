"""AgentCore Platform v1.0 — FIN-C2-004 PostProcessNode"""

# External-output boundary for the summary report. Three independent layers,
# in order:
#
#   (1) whole-document redaction — the report is a reduction of the source
#       document, never the document itself: a verbatim embedding of the full
#       validated document text in the report is replaced with [REDACTED],
#       with an audit event;
#   (2) credential / PII scan — API keys, bearer tokens, private-key headers,
#       SSN/credit-card/email/phone patterns anywhere in the report withhold
#       the output entirely (status ERROR). This runs BEFORE the numeric
#       layer: the precision grammar deliberately treats any standalone
#       3-letter uppercase word as a currency code (a false snap fails safe),
#       so snapping first would rewrite digits inside a labelled sequence
#       such as "SSN 123-45-6789" and hide it from this scan;
#   (3) monetary precision grid — the documented external schema expresses
#       monetary figures in units of 1,000; every monetary-form token is
#       snapped onto that grid (off-grid values are full-precision figures
#       leaking to the external surface), with an audit event per pass. The
#       scan in (2) is repeated afterwards so no gate-internal rewrite can
#       ever produce an unscanned surface.
#
# The domain output gate is implemented as module-level functions called from
# inside execute() — NOT instance methods on the node class (the framework's
# own output gate methods are final and must not be shadowed).
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

# Credential patterns that MUST NOT appear in the external report.
_CREDENTIAL_PATTERNS = re.compile(
    r"(password\s*=\s*\S+"
    r"|api[_-]?key\s*[:=]\s*\S+"
    r"|bearer\s+[A-Za-z0-9+/=]{20,}"
    r"|-----BEGIN\s+(RSA|EC|PRIVATE)"
    r"|\bAKIA[A-Z0-9]{16}\b)",  # AWS access key pattern
    re.IGNORECASE,
)

_PII_PATTERNS = re.compile(
    r"(\b\d{3}-\d{2}-\d{4}\b"  # SSN
    r"|\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"  # credit card
    r"|\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"  # email
    r"|\b\d{3}[\s.\-]?\d{3}[\s.\-]?\d{4}\b)",  # phone
)

# Minimum length at which a verbatim embedding of the whole source document in
# the report is treated as a leak (very short documents cannot meaningfully be
# "reduced", and whitespace-normalised summaries never reproduce them byte-
# for-byte anyway).
_VERBATIM_DOC_MIN_CHARS = 200

# Approved external precision: monetary figures are expressed in units of
# 1,000 (must match the schema note this node appends to the report and the
# aggregates rendered by src/nodes/summarize_node.py — the report RENDERS on
# this grid, this gate ENFORCES it).
_EXTERNAL_ROUND_UNIT = 1000
# EXPLICIT output schema — monetary values are identified by FORM and by
# CURRENCY CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567) and unformatted runs
#            of 5+ digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency marker is
#            monetary even though short — SYMMETRICALLY: a 3-letter uppercase
#            code or a currency symbol (incl. fullwidth ￥ and 円/₩), before or
#            after the value, attached or separated by ANY whitespace run
#            (spaces, tabs, newlines — the delimiter grammar is `\s*`),
#            signed or unsigned. Word-boundary guards keep embedded acronyms
#            structural ("STAR 2026" does not match — the 3 letters must be a
#            standalone word). Any standalone 3-letter uppercase word counts
#            as a code on purpose: a false snap fails SAFE while a missed
#            leak does not.
# Structural tokens stay untouched: page references ("p.21"), version tags
# ("v12"), bare counts, years without currency adjacency ("in 2026").
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision figure reaching the external surface,
# snapped + audited.
# Grammar (group-based; no lookbehinds, so the delimiter can be ARBITRARY
# whitespace — spaces, tabs, newlines, any run length). Every value accepts
# an optional explicit +/- sign.
# Branch order matters: currency-context branches first, then form-based.
_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"
# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position").
# Every enumerated leak form (spaces, tabs, single newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A monetary amount may carry a DECIMAL part, and every value alternative absorbs
# it into the SAME token. Two separate things go wrong without that:
#   - the fraction of "9999.99999" is a standalone 5+-digit run in its own
#     right, so the form-based branch latches onto it and rewrites a percentage
#     into "9999.100,000" — a number the document never contained;
#   - in currency context the integer part snaps while the fraction dangles
#     ("JPY 1234.56" -> "JPY 1,000.56"), producing a value that is neither the
#     true figure nor on the grid.
# The fix is NOT to exempt decimals from the grid. An off-grid amount in
# explicit currency context still snaps — as ONE number ("JPY 1234.56" ->
# "JPY 1,000") — while a ratio, percentage, version or page reference, which was
# never a monetary token, is left byte-identical.
#
# The `(?!\.\d)` arm is what makes absorption stick. A plain `(?:\.\d+)?` lets
# the engine backtrack out of the fraction and re-match the integer part alone
# whenever the text right after the fraction fails the token's trailing guard —
# "JPY 1234.56m" would go back to matching "JPY 1234" and the dangling-fraction
# bug returns. Either the fraction is taken whole, or there is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

# LEADING guard: a match may not BEGIN immediately after a digit or a decimal
# point, so no alternative can enter a number part-way through and treat the
# tail of a fraction as a value of its own ("0.123456" can be entered neither at
# "123456" nor at "23456"). It is deliberately a leading guard ONLY — putting
# "." in a trailing guard would let an amount that ends a sentence escape the
# grid ("The book totals JPY 9999.").
_NUM_INTERIOR = r"(?<![\d.])"

_NUM_TOKEN_RE = re.compile(
    _NUM_INTERIOR
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value
    # — an on-grid "JPY 1,000" must stay byte-identical, and an off-grid
    # "JPY 1,234" must snap as 1234, not as 1.
    + rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})\b"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))"
)


def _enforce_precision(result: str) -> tuple[str, int]:
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a
    full-precision monetary figure reached the external surface — the gate
    rounds it onto the approved grid. The currency marker, the original
    delimiter whitespace, and the explicit sign of the original token are all
    preserved on the snapped replacement.
    """
    redactions = 0

    def _snap(match: re.Match[str]) -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # whole amount — not just its integer part — is what sits on the grid.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, result), redactions


_SCHEMA_NOTE = (
    "_All monetary amounts in this report are rounded to the nearest "
    "1,000 currency units; itemized amounts from the source document are "
    "reported as aggregates._"
)


def _redact_verbatim_document(formatted: str, document: str) -> tuple[str, int]:
    """Replace any verbatim embedding of the whole source document.

    The report must be a reduction of the document; echoing the full
    validated text wholesale is a leak regardless of its content. Returns
    (sanitised_result, redaction_count).
    """
    if len(document) < _VERBATIM_DOC_MIN_CHARS or document not in formatted:
        return formatted, 0
    count = formatted.count(document)
    return formatted.replace(document, "[REDACTED]"), count


def _security_gate_output(text: str) -> str | None:
    """Scan the final report for credential / PII patterns.

    Returns the violation class name, or None if the output is clean.
    """
    if _CREDENTIAL_PATTERNS.search(text):
        return "credential_pattern"
    if _PII_PATTERNS.search(text):
        return "pii_pattern"
    return None


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Format and finalize the summarization report behind the output gate.

    Reads: summary_output, key_figures, risk_flags, action_items,
           document_type, fiscal_period, validated_input
    Writes: formatted_output (the field the graph surfaces as the result)
    Gate: whole-document redaction -> credential/PII scan -> monetary
          precision grid -> credential/PII re-scan, each audited.
    """

    # Formatting / output node — no external caller gating required
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    @staticmethod
    def _blocked(violation: str, state: dict[str, Any]) -> dict[str, Any]:
        """Withhold the report: the output gate found a disallowed pattern."""
        emit_trace_event(
            "post_process_blocked",
            {"reason": "output_gate_violation", "violation": violation},
            state,
        )
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PostProcessNode: output gate — {violation} detected in report"],
            "error_message": "Output withheld: security gate violation.",
        }

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A run declined upstream has no summary to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "output": message,
                "formatted_output": message,
            }
        summary_output = state.get("summary_output", "")
        key_figures = state.get("key_figures", "")
        risk_flags = state.get("risk_flags", "")
        action_items = state.get("action_items", "")
        document_type = state.get("document_type", "")
        fiscal_period = state.get("fiscal_period", "")

        # ── Assemble the report ───────────────────────────────────────────
        header_bits = [bit for bit in (document_type, fiscal_period) if bit]
        sections = []
        if header_bits:
            sections.append(f"# Summary Report ({' / '.join(header_bits)})")
        if summary_output:
            sections.append(f"## Executive Summary\n{summary_output}")
        if key_figures:
            sections.append(f"## Key Figures\n{key_figures}")
        if risk_flags:
            sections.append(f"## Risk Flags\n{risk_flags}")
        if action_items:
            sections.append(f"## Action Items\n{action_items}")

        if not (summary_output or key_figures or risk_flags or action_items):
            formatted = "(No summary generated.)"
        else:
            sections.append(_SCHEMA_NOTE)
            formatted = "\n\n".join(sections)

        # ── Layer 1: whole-document verbatim redaction ────────────────────
        document = state.get("validated_input", "") or ""
        if isinstance(document, str):
            formatted, verbatim_redactions = _redact_verbatim_document(formatted, document)
            if verbatim_redactions:
                emit_trace_event(
                    "output_verbatim_redaction",
                    {"redactions": verbatim_redactions},
                    state,
                )

        # ── Layer 2: credential / PII scan (before any numeric rewrite) ───
        violation = _security_gate_output(formatted)
        if violation is not None:
            return self._blocked(violation, state)

        # ── Layer 3: monetary precision grid ──────────────────────────────
        formatted, precision_redactions = _enforce_precision(formatted)
        if precision_redactions:
            emit_trace_event(
                "output_precision_redaction",
                {"redactions": precision_redactions},
                state,
            )

        # Re-scan the rewritten surface: snapping cannot introduce a secret,
        # but the final bytes that leave the agent are never unscanned.
        violation = _security_gate_output(formatted)
        if violation is not None:
            return self._blocked(violation, state)

        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(formatted),
                "precision_redactions": precision_redactions,
            },
            state,
        )
        return {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
