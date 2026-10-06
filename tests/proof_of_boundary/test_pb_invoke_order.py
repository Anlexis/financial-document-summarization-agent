# PB-6: Backbone Invoke-Order Verification — FIN-C2-004
#
# Verifies that a full `FinC2004Agent().invoke()` with a SUCCESS-yielding payload
# runs the 5-node backbone in the correct order:
#   InitializeNode → PreProcessNode → SummarizeNode → PostProcessNode → FinalizeNode
#
# The caller MUST use TrustLevel.VERIFIED_EXTERNAL (PreProcessNode.required_trust_level)
# to exercise the real trust path — an INTERNAL context would mask a trust-trap.

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.schemas.agent_status import AgentStatus
from src.graph.graph import FinC2004Agent

# The class name registered in the `main` slot
_MAIN_SLOT_NODE = "SummarizeNode"

# A payload that yields SUCCESS through the full pipeline (no PII, no injection,
# under length limit, valid financial text).
#
# IMPORTANT: the real SDK's _security_gate_input() uses detect_pii() which treats
# two or more consecutive Title-Case words as a personal-name heuristic (high-recall,
# low-precision).  Pairs like "Financial Report", "Acme Corp", "Net Income" match
# and get masked to [MASKED]; our execute() then sees the sentinel and blocks the
# request.  This payload deliberately avoids consecutive Title-Case word pairs —
# each sentence uses at most one capitalised word (the first) so detect_pii() sees
# zero findings (verified: detect_pii(payload) == []).
_VALID_PAYLOAD = (
    "Revenue grew year-over-year driven by expansion across all market segments. "
    "Net income improved on operational efficiency gains in the quarter. "
    "Key risks include market volatility and supply-chain disruption. "
    "The board approved the capital expenditure plan for the coming fiscal year."
)

_EXPECTED_ORDER = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,
    "PostProcessNode",
    "FinalizeNode",
]


@pytest.fixture(autouse=True)
def patch_audit(monkeypatch):
    """Suppress emit_trace_event side effects across all nodes and framework."""
    monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.chunk_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.summarize_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


class TestPB6BackboneInvokeOrder:
    """PB-6: Full backbone invoke must run 5 nodes in the correct order."""

    def test_backbone_invoke_order_success(self):
        """PB-6: FinC2004Agent.invoke() runs backbone nodes in expected order."""
        agent = FinC2004Agent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(user_input=_VALID_PAYLOAD, ctx=ctx)

        # The invoke result must be SUCCESS (a non-SUCCESS status short-circuits
        # the main→post_process leg and would make the order assertion meaningless)
        assert result["status"] == AgentStatus.SUCCESS.value, (
            f"Expected SUCCESS but got {result['status']}. " f"Ensure _VALID_PAYLOAD passes all PreProcessNode checks."
        )

        # Output must be non-empty (PostProcessNode writes formatted_output)
        assert result["output"], "result['output'] must be non-empty — PostProcessNode must write formatted_output"

        # Verify backbone node history order
        node_history = result.get("node_history", [])
        assert node_history, "node_history must be non-empty after a full backbone invoke"

        # Extract class names (node_history entries may be strings or class instances)
        def _name(entry) -> str:
            if isinstance(entry, str):
                return entry
            return getattr(entry, "__name__", type(entry).__name__)

        history_names = [_name(e) for e in node_history]

        for expected_node in _EXPECTED_ORDER:
            assert any(expected_node in name for name in history_names), (
                f"Expected '{expected_node}' in node_history but not found.\n" f"Full history: {history_names}"
            )

        # Verify ORDER: each expected node must appear after the previous one
        positions = []
        for expected_node in _EXPECTED_ORDER:
            for idx, name in enumerate(history_names):
                if expected_node in name:
                    positions.append(idx)
                    break

        assert positions == sorted(positions), (
            f"Backbone nodes are out of order.\n"
            f"Expected order: {_EXPECTED_ORDER}\n"
            f"Actual history: {history_names}"
        )
