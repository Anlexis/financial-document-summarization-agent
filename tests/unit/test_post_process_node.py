# FIN-C2-004 — Unit Tests: PostProcessNode
# The external-output boundary: report formatting, whole-document redaction,
# the monetary precision grid (enforced for EVERY representation), and the
# credential/PII block-scan. Boundary tests run BOTH ways: every leak form
# snaps, structural tokens stay byte-identical.

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.post_process_node import PostProcessNode, _enforce_precision


@pytest.fixture(autouse=True)
def patch_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides):
    state = {
        "summary_output": "Revenue grew this quarter on broad demand.",
        "key_figures": "Monetary amounts detected: 2. Material amounts (absolute, rounded): total ≈ 2,125,000; largest ≈ 1,235,000.",
        "risk_flags": "market risk (1 mention).",
        "action_items": "- The board approved the plan.",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
    }
    state.update(overrides)
    return state


class TestPostProcessFormatting:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_full_report_sections_and_schema_note(self):
        """TC-POST-001: All sections render plus the precision schema note."""
        result = self.node(_state(document_type="annual_report", fiscal_period="fy2026_q3"))
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert "# Summary Report (annual_report / fy2026_q3)" in out
        assert "## Executive Summary" in out
        assert "## Key Figures" in out
        assert "## Risk Flags" in out
        assert "## Action Items" in out
        assert "rounded to the nearest" in out

    def test_empty_fields_yield_placeholder(self):
        """TC-POST-002: No summary fields -> placeholder output."""
        result = self.node(_state(summary_output="", key_figures="", risk_flags="", action_items=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == "(No summary generated.)"

    def test_partial_fields_render_only_their_sections(self):
        """TC-POST-003: Only populated sections appear."""
        result = self.node(_state(risk_flags="", action_items=""))
        out = result["formatted_output"]
        assert "## Executive Summary" in out
        assert "## Key Figures" in out
        assert "## Risk Flags" not in out
        assert "## Action Items" not in out

    def test_trust_level_is_anonymous(self):
        assert PostProcessNode.required_trust_level == TrustLevel.ANONYMOUS


class TestOutputBlockScan:
    """Credential / PII anywhere in the final report withholds the output."""

    def setup_method(self):
        self.node = PostProcessNode()

    def _assert_blocked(self, result):
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result
        assert result["error_log"]

    def test_api_key_in_summary_blocks_output(self):
        """TC-POST-004"""
        self._assert_blocked(self.node(_state(summary_output="Config leak api_key: sk-abc123def456ghi789")))

    def test_bearer_token_in_key_figures_blocks_output(self):
        """TC-POST-005"""
        self._assert_blocked(self.node(_state(key_figures="Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef")))

    def test_email_in_risk_flags_blocks_output(self):
        """TC-POST-006"""
        self._assert_blocked(self.node(_state(risk_flags="Contact auditor@example.com")))

    def test_ssn_in_action_items_blocks_output(self):
        """TC-POST-007"""
        self._assert_blocked(self.node(_state(action_items="- Verify SSN 123-45-6789")))

    @pytest.mark.parametrize(
        "labelled",
        [
            "- Verify SSN 123-45-6789",  # "SSN" reads as a currency code
            "Ref TAX 123-45-6789 on file",  # any 3-letter uppercase label
            "Card VSA 4111 1111 1111 1111",
        ],
    )
    def test_labelled_pii_is_scanned_before_numeric_rewriting(self, labelled):
        """The precision grammar treats any standalone 3-letter uppercase word
        as a currency marker (a false snap fails safe). If the numeric layer
        ran first it would rewrite digits inside a labelled PII sequence and
        hide it from the scan — so the scan must see the pre-snap text."""
        self._assert_blocked(self.node(_state(action_items=labelled)))


class TestWholeDocumentRedaction:
    """The report is a reduction — the full validated document embedded
    verbatim is redacted, with the rest of the report intact."""

    def setup_method(self):
        self.node = PostProcessNode()

    def test_full_document_embedding_is_redacted(self):
        document = ("Revenue held steady across every segment this period. " * 5).strip()
        assert len(document) >= 200
        result = self.node(_state(summary_output=document, validated_input=document))
        out = result["formatted_output"]
        assert document not in out
        assert "[REDACTED]" in out
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_short_document_quotes_are_not_redacted(self):
        document = "Revenue held steady."
        result = self.node(_state(summary_output=document, validated_input=document))
        assert "[REDACTED]" not in result["formatted_output"]


class TestPrecisionGridEnforcement:
    """The stated invariant — ALL monetary-form tokens sit on the 1,000 grid —
    is enforced for every representation: grouped, bare 5+-digit, currency
    context on either side, signed, arbitrary whitespace runs. No magnitude
    exemptions. Structural tokens stay byte-identical."""

    # ── every leak form snaps ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "leak, expected",
        [
            ("JPY 1,234", "JPY 1,000"),  # marker-adjacent grouped value snaps whole
            ("JPY 9999", "JPY 10,000"),  # short value in currency context
            ("9999 JPY", "10,000 JPY"),  # symmetric: value then marker
            ("JPY  9999", "JPY  10,000"),  # multi-space delimiter preserved
            ("JPY\t9999", "JPY\t10,000"),  # tab delimiter
            ("USD\n9999", "USD\n10,000"),  # newline delimiter
            ("¥9999", "¥10,000"),  # attached symbol
            ("￥9999", "￥10,000"),  # fullwidth symbol
            ("9999円", "10,000円"),  # postfix kanji marker
            ("9999₩", "10,000₩"),  # won symbol
            ("JPY-9999", "JPY-10,000"),  # signed, attached
            ("JPY +9999", "JPY +10,000"),  # signed, spaced, sign preserved
            ("-9999 JPY", "-10,000 JPY"),  # sign before value-then-marker
            ("9,999", "10,000"),  # grouped form below 10,000 — no magnitude exemption
            ("12,345,678", "12,346,000"),  # large grouped form
            ("123456", "123,000"),  # bare 5+-digit run
            ("+123456", "+123,000"),  # signed bare run
        ],
    )
    def test_leak_forms_snap(self, leak, expected):
        out, redactions = _enforce_precision(leak)
        assert out == expected
        assert redactions == 1

    # ── on-grid and structural tokens stay byte-identical ────────────────────

    @pytest.mark.parametrize(
        "clean",
        [
            "JPY 1,000",  # on-grid grouped value
            "JPY 10,000",
            "125,000 USD",
            "STAR 2026",  # embedded acronym + year: structural
            "in 2026",  # bare year
            "90d horizon",  # horizon token
            "p.21",  # page reference
            "v12",  # version tag
            "42 items",  # bare count
            "15%",  # percentage
        ],
    )
    def test_structural_and_on_grid_tokens_untouched(self, clean):
        out, redactions = _enforce_precision(clean)
        assert out == clean
        assert redactions == 0

    # ── node-level enforcement on the assembled report ───────────────────────

    def test_off_grid_figures_in_report_are_snapped(self):
        node = PostProcessNode()
        result = node(_state(summary_output="Revenue grew to JPY 1,234,567 in the quarter."))
        out = result["formatted_output"]
        assert "1,234,567" not in out
        assert "JPY 1,235,000" in out
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_on_grid_report_passes_byte_identical_sections(self):
        summary = "Capital spend of JPY 890,000 was approved."
        node = PostProcessNode()
        result = node(_state(summary_output=summary))
        assert summary in result["formatted_output"]

    def test_entire_report_scans_on_grid(self):
        """On-grid scan of the final surface with the gate's own recogniser."""
        from src.nodes.post_process_node import _NUM_TOKEN_RE

        node = PostProcessNode()
        result = node(
            _state(
                summary_output="Revenue grew to JPY 1,234,567; costs of 98765 were recorded.",
                key_figures="total ≈ 2,124,567; largest ≈ 1,234,567.",
            )
        )
        out = result["formatted_output"]
        for match in _NUM_TOKEN_RE.finditer(out):
            token = match.group("val_after") or match.group("val_before") or match.group("val_form")
            assert int(token.replace(",", "")) % 1000 == 0, f"off-grid token in output: {match.group(0)!r}"


class TestDecimalPrecisionGrid:
    """Decimals, both directions.

    The grammar once treated the fraction of a decimal as a value of its own:
    a 5+-digit fraction is a standalone leak form, and nothing stopped a match
    from starting inside one. Two distinct wrong answers came out of that —
    a ratio or percentage the report never claimed as money was rewritten
    ("8.512345" -> "8.512,000"), and an amount in real currency context had its
    integer part snapped while the fraction dangled ("JPY 1234.56" ->
    "JPY 1,000.56"), a figure that is neither true nor on the grid.

    The fix is NOT a decimal exemption. An off-grid amount in explicit currency
    context still snaps — as ONE number — and only tokens that were never
    monetary survive byte-identical."""

    # ── decimal AMOUNTS still snap, as one number ────────────────────────────

    @pytest.mark.parametrize(
        "leak, expected",
        [
            ("JPY 1234.56", "JPY 1,000"),  # the dangling-fraction case
            ("JPY  1234.56", "JPY  1,000"),  # delimiter run preserved
            ("USD\n1234.56", "USD\n1,000"),  # single-newline delimiter preserved
            ("1234.56 JPY", "1,000 JPY"),  # symmetric: value then marker
            ("¥1234.56", "¥1,000"),  # attached symbol
            ("1234.56円", "1,000円"),  # postfix kanji marker
            ("JPY -1234.56", "JPY -1,000"),  # signed
            ("+1234.56 JPY", "+1,000 JPY"),  # explicit plus preserved
            ("1,234.567", "1,000"),  # grouped form with a fraction
            ("98765.4321", "99,000"),  # bare 5+-digit run with a fraction
            ("JPY 1500.5", "JPY 2,000"),  # rounds on the whole value, not the integer part
        ],
    )
    def test_decimal_amounts_snap_as_one_number(self, leak, expected):
        out, redactions = _enforce_precision(leak)
        assert out == expected
        assert redactions == 1

    def test_amount_ending_a_sentence_still_snaps(self):
        """The decimal point joins the LEADING guard only. In a trailing guard
        it would make the full stop part of the token's context and let an
        amount that ends a sentence walk off the grid."""
        out, redactions = _enforce_precision("The book totals JPY 9999.")
        assert out == "The book totals JPY 10,000."
        assert redactions == 1

    # ── decimals that were never money stay byte-identical ───────────────────

    @pytest.mark.parametrize(
        "clean",
        [
            "8.512345",  # bare decimal, 6-digit fraction
            "9999.99999%",  # percentage with a long fraction
            "ratio 0.123456",  # bare decimal after a word
            "12.5%",  # ordinary percentage
            "current ratio 1.234567 at period end",  # ratio in prose
            "section 3.14159",  # long fraction after a word
            "v1.234567",  # version tag with a fraction
            "p.21",  # page reference
            "2026-08-26",  # ISO date
            "JPY 1,000.00",  # on-grid amount carrying a zero fraction
            "JPY 2,000.000",  # on-grid amount, longer zero fraction
        ],
    )
    def test_non_monetary_decimals_are_untouched(self, clean):
        out, redactions = _enforce_precision(clean)
        assert out == clean
        assert redactions == 0

    def test_fraction_is_absorbed_not_reentered(self):
        """A match must never BEGIN inside a number. Guarding only the decimal
        point is not enough: with `(?<!\\.)` alone the engine simply enters the
        fraction one digit later and "0.123456" still comes out mangled."""
        out, _ = _enforce_precision("ratio 0.123456 and 0.9876543 recorded")
        assert out == "ratio 0.123456 and 0.9876543 recorded"

    def test_decimal_glued_to_a_word_does_not_leave_a_dangling_fraction(self):
        """Absorption has to stick under backtracking. A plain optional
        fraction lets the engine fall back to the integer part when the text
        after the fraction fails the token's trailing guard, which puts
        "JPY 1,000.56m" straight back on the wire."""
        out, _ = _enforce_precision("JPY 1234.56m")
        assert ".56" not in out or out == "JPY 1234.56m"
        assert out == "JPY 1234.56m"

    # ── node-level: a caller decimal must not be rewritten in the report ─────

    def test_report_keeps_caller_ratios_and_snaps_caller_amounts(self):
        node = PostProcessNode()
        result = node(
            _state(
                summary_output=(
                    "Operating margin improved to 12.5% and the current ratio stands at 1.234567. "
                    "Revenue reached JPY 1234.56 million."
                ),
                key_figures="",
            )
        )
        out = result["formatted_output"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "12.5%" in out
        assert "1.234567" in out  # a ratio is not a monetary figure
        assert "JPY 1,000 million" in out  # the amount snapped as ONE number
        assert "1,000.56" not in out  # ... with no fraction left dangling
        assert "1.235,000" not in out  # ... and no spliced pseudo-number

    def test_no_decimal_splice_artifact_anywhere_in_the_report(self):
        """The corruption signature: a decimal point immediately followed by a
        comma-grouped run ("1.235,000"). It cannot occur on a clean surface."""
        import re

        node = PostProcessNode()
        result = node(
            _state(
                summary_output=("EPS of JPY 8.512345, margin 9999.99999%, ratio 0.123456, revenue JPY 1234.56."),
                key_figures="",
            )
        )
        out = result["formatted_output"]
        assert re.search(r"\d\.\d+,\d{3}", out) is None, f"decimal splice artifact in output: {out!r}"
