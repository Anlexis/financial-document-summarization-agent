"""AgentCore Platform v1.0 — FIN-C2-004 ChunkNode"""

# Splits the validated financial document into overlapping text segments
# suitable for LLM context-window constraints.

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_CHUNK_SIZE = 2000  # characters per segment
_OVERLAP = 200  # overlap between adjacent segments


def chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _OVERLAP) -> list[str]:
    """Split *text* into overlapping segments.

    Returns a list of str segments. Each segment is at most *chunk_size*
    characters; consecutive segments overlap by *overlap* characters.
    """
    if not text:
        return []

    segments: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        segments.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return segments


class ChunkNode(FunctionNode):
    """Split validated_input into overlapping segments for LLM context compliance.

    Reads: validated_input (str)
    Writes: chunked_segments (list[str])
    Emits a chunk_complete audit event.
    """

    # Inner processing node — no external caller gating required
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        text = state.get("validated_input", state.get("input_text", ""))

        if not text:
            emit_trace_event(
                "chunk_error",
                {"reason": "no_validated_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ChunkNode: no validated_input found in state"],
            }

        segments = chunk_text(text)

        emit_trace_event(
            "chunk_complete",
            {"segment_count": len(segments), "total_chars": len(text)},
            state,
        )
        return {
            "chunked_segments": segments,
            "status": AgentStatus.SUCCESS.value,
        }
