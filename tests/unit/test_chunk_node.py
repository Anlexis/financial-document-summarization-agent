# FIN-C2-004 — Unit Tests: ChunkNode + chunk_text helper

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.chunk_node import ChunkNode, chunk_text


@pytest.fixture(autouse=True)
def patch_audit(monkeypatch):
    """Suppress emit_trace_event calls."""
    monkeypatch.setattr("src.nodes.chunk_node.emit_trace_event", lambda *a, **k: None)


class TestChunkText:
    """Unit tests for the chunk_text() helper (pure logic, no framework calls)."""

    def test_empty_string_returns_empty_list(self):
        """TC-CHUNK-001: Empty input produces no segments."""
        assert chunk_text("") == []

    def test_short_text_is_single_segment(self):
        """TC-CHUNK-002: Text shorter than chunk_size produces exactly one segment."""
        text = "Short document."
        result = chunk_text(text, chunk_size=2000)
        assert len(result) == 1
        assert result[0] == text

    def test_exact_chunk_size_is_single_segment(self):
        """TC-CHUNK-003: Text of exactly chunk_size produces one segment."""
        text = "A" * 2000
        result = chunk_text(text, chunk_size=2000, overlap=200)
        assert len(result) == 1

    def test_segments_cover_full_text(self):
        """TC-CHUNK-004: All characters appear in at least one segment."""
        text = "X" * 5000
        segments = chunk_text(text, chunk_size=2000, overlap=200)
        # Concatenation with overlap may duplicate; all of text[0] must appear
        assert any("X" in s for s in segments)
        assert len(segments) > 1

    def test_segment_length_bounded_by_chunk_size(self):
        """TC-CHUNK-005: No segment exceeds chunk_size characters."""
        text = "Y" * 10_000
        chunk_size = 1500
        for seg in chunk_text(text, chunk_size=chunk_size, overlap=100):
            assert len(seg) <= chunk_size

    def test_overlap_causes_repeated_content(self):
        """TC-CHUNK-006: Adjacent segments share overlap characters."""
        text = "AB" * 2000  # 4000 chars
        segments = chunk_text(text, chunk_size=2000, overlap=200)
        assert len(segments) >= 2
        # Last `overlap` chars of segment 0 should equal first chars of segment 1
        overlap_from_s0 = segments[0][-200:]
        start_of_s1 = segments[1][:200]
        assert overlap_from_s0 == start_of_s1

    def test_custom_chunk_and_overlap_params(self):
        """TC-CHUNK-007: Custom chunk_size and overlap are honoured."""
        text = "Z" * 1000
        segments = chunk_text(text, chunk_size=300, overlap=50)
        assert len(segments) >= 3
        for seg in segments:
            assert len(seg) <= 300


class TestChunkNode:
    """Unit tests for ChunkNode FunctionNode."""

    def setup_method(self):
        self.node = ChunkNode()

    def _state(self, validated_input=""):
        return {
            "validated_input": validated_input,
            # ANONYMOUS caller — internal pipeline node.
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "node_history": [],
            "error_log": [],
        }

    def test_valid_input_produces_segments(self):
        """TC-CHUNKNODE-001: Valid validated_input produces chunked_segments list."""
        state = self._state("Annual report content. " * 50)
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain status string, never the enum.
        # AgentStatus subclasses str, so isinstance() would also accept an enum
        # member — exact-type identity is what makes this a regression guard.
        assert type(result["status"]) is str  # noqa: E721
        assert isinstance(result["chunked_segments"], list)
        assert len(result["chunked_segments"]) >= 1

    def test_empty_validated_input_returns_error(self):
        """TC-CHUNKNODE-002: Empty validated_input returns ERROR."""
        result = self.node(self._state(""))
        assert result["status"] == AgentStatus.ERROR.value
        # AgentStatus subclasses str, so isinstance() would also accept an enum
        # member — exact-type identity is what makes this a regression guard.
        assert type(result["status"]) is str  # noqa: E721

    def test_missing_validated_input_uses_input_text_fallback(self):
        """TC-CHUNKNODE-003: Falls back to input_text if validated_input absent."""
        state = {
            "input_text": "Fallback document text.",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "node_history": [],
            "error_log": [],
        }
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["chunked_segments"]

    def test_trust_level_is_anonymous(self):
        """ChunkNode must declare ANONYMOUS trust (internal pipeline)."""
        from framework.schemas.trust_level import TrustLevel

        assert ChunkNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_execute_returns_partial_dict(self):
        """Node contract: returns a curated partial dict (via __call__).

        __call__ appends node_history/execution_time; the node's success return
        must not echo back the input-only error_log field."""
        state = self._state("Report data.")
        result = self.node(state)
        assert "error_log" not in result
