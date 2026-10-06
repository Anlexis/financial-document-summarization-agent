# FIN-C2-004 — Unit Tests: SummarizeNode
# The summary is COMPUTED from the validated document: extractive executive
# summary, aggregate-only key figures (materiality-filtered, grid-rounded),
# lexicon risk flags (+ caller focus terms), directive-sentence action items.

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.summarize_node import SummarizeNode

_DOC = (
    "Revenue grew to JPY 1,234,567 in the quarter driven by expansion across market segments. "
    "Net income improved on operational efficiency gains and margins rose 15%. "
    "Key risks include market volatility and supply-chain disruption. "
    "A minor petty-cash variance of JPY 500 was recorded. "
    "The board approved a capital expenditure plan of JPY 890,000 for the coming fiscal year."
)


@pytest.fixture(autouse=True)
def patch_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.summarize_node.emit_trace_event", lambda *a, **k: None)


class TestSummarizeNode:
    def setup_method(self):
        self.node = SummarizeNode()

    def _state(self, **overrides):
        state = {
            "validated_input": _DOC,
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "node_history": [],
            "error_log": [],
        }
        state.update(overrides)
        return state

    # ── Real computed output ──────────────────────────────────────────────────

    def test_all_fields_computed_from_document(self):
        """TC-SUM-001: All four summary fields are non-empty strings."""
        result = self.node(self._state())
        assert result["status"] == AgentStatus.SUCCESS.value
        for field in ("summary_output", "key_figures", "risk_flags", "action_items"):
            assert isinstance(result[field], str) and result[field]

    def test_output_reflects_the_input_not_a_fixed_template(self):
        """Different documents must produce different summaries."""
        doc_a = "Revenue rose to JPY 50,000 this quarter. Litigation risk increased."
        doc_b = "Cash reserves fell by JPY 70,000. The board will review liquidity funding pressure."
        result_a = self.node(self._state(validated_input=doc_a))
        result_b = self.node(self._state(validated_input=doc_b))
        assert result_a["summary_output"] != result_b["summary_output"]
        assert result_a["key_figures"] != result_b["key_figures"] or result_a["risk_flags"] != result_b["risk_flags"]

    def test_deterministic_for_identical_input(self):
        first = self.node(self._state())
        second = self.node(self._state())
        for field in ("summary_output", "key_figures", "risk_flags", "action_items"):
            assert first[field] == second[field]

    # ── Key-figure aggregation arithmetic ─────────────────────────────────────

    def test_key_figures_aggregates_with_materiality_filter(self):
        """Amounts: 1,234,567 / 500 / 890,000. Threshold 10,000 keeps two.

        total  = 1,234,567 + 890,000 = 2,124,567 -> rendered 2,125,000
        largest= 1,234,567           -> rendered 1,235,000
        """
        result = self.node(self._state(materiality_threshold=10_000.0))
        kf = result["key_figures"]
        assert "Monetary amounts detected: 3" in kf
        assert "(2 at or above the materiality threshold)" in kf
        assert "total ≈ 2,125,000" in kf
        assert "largest ≈ 1,235,000" in kf

    def test_key_figures_zero_threshold_counts_all(self):
        result = self.node(self._state())
        kf = result["key_figures"]
        assert "Monetary amounts detected: 3 (3 at or above the materiality threshold)" in kf
        # total = 1,234,567 + 890,000 + 500 = 2,125,067 -> rendered 2,125,000
        assert "total ≈ 2,125,000" in kf

    def test_key_figures_rendered_values_sit_on_the_grid(self):
        """Every rendered aggregate is pre-rounded to the nearest 1,000."""
        import re

        result = self.node(self._state(materiality_threshold=10_000.0))
        for token in re.findall(r"\d[\d,]*", result["key_figures"]):
            value = int(token.replace(",", ""))
            if value >= 1000:
                assert value % 1000 == 0, f"off-grid aggregate rendered: {token}"

    def test_key_figures_without_amounts(self):
        result = self.node(self._state(validated_input="Margins were stable. Outlook is unchanged."))
        assert "No monetary amounts detected" in result["key_figures"]

    def test_percentage_count_reported(self):
        result = self.node(self._state())
        assert "Percentage figures detected: 1." in result["key_figures"]

    # ── Risk flags ────────────────────────────────────────────────────────────

    def test_risk_lexicon_categories_counted(self):
        result = self.node(self._state())
        assert "market risk" in result["risk_flags"]
        assert "operational risk" in result["risk_flags"]

    def test_overlapping_phrases_count_once(self):
        """'market volatility' is ONE market-risk mention, not two."""
        result = self.node(self._state(validated_input="The outlook cites market volatility this year."))
        assert "market risk (1 mention)" in result["risk_flags"]

    def test_focus_terms_matched_with_separator_variants(self):
        """'supply_chain' matches the document's hyphenated 'supply-chain'."""
        result = self.node(self._state(focus_terms=["supply_chain", "liquidity"]))
        assert "focus term 'supply_chain' (1 mention)" in result["risk_flags"]
        assert "focus term 'liquidity' (0 mentions)" in result["risk_flags"]

    def test_no_risk_terms(self):
        result = self.node(self._state(validated_input="Totals were unchanged from the prior period."))
        assert result["risk_flags"] == "No risk-related terms detected."

    # ── Action items ──────────────────────────────────────────────────────────

    def test_action_items_capture_directive_sentences(self):
        result = self.node(self._state())
        assert "- The board approved a capital expenditure plan" in result["action_items"]

    def test_action_items_absent(self):
        result = self.node(self._state(validated_input="Totals were unchanged. Margins held."))
        assert result["action_items"] == "No explicit action items detected."

    # ── Input fallbacks / error path ──────────────────────────────────────────

    def test_fallback_to_chunked_segments(self):
        """TC-SUM-004: With no validated_input, pre-chunked segments are used."""
        result = self.node(self._state(validated_input="", chunked_segments=["Revenue rose.", "Costs fell."]))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["summary_output"]

    def test_no_input_at_all_returns_error(self):
        """TC-SUM-005: Neither text nor segments -> error."""
        result = self.node(
            {
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "node_history": [],
                "error_log": [],
            }
        )
        assert result["status"] == AgentStatus.ERROR.value

    def test_chunked_segments_written(self):
        result = self.node(self._state())
        assert isinstance(result["chunked_segments"], list) and result["chunked_segments"]

    # ── Defensive metadata handling (direct-call hardening) ───────────────────

    def test_non_numeric_threshold_in_state_degrades_to_zero(self):
        """State-level junk (bypassing PreProcessNode) must not break the node."""
        result = self.node(self._state(materiality_threshold="not a number"))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_list_focus_terms_in_state_degrades_to_empty(self):
        result = self.node(self._state(focus_terms="liquidity"))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_trust_level_is_anonymous(self):
        assert SummarizeNode.required_trust_level == TrustLevel.ANONYMOUS
