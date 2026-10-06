"""AgentCore Platform v1.0"""

# The framework package ships without a py.typed marker, so AgentState
# resolves to Any for a type checker and this class is not recognised as a
# TypedDict — which makes NotRequired[] unverifiable HERE. At runtime
# AgentState is a genuine TypedDict and these fields must stay NotRequired
# (they are written by nodes, not present at graph initialisation), so the
# annotations are kept and only that one check is disabled.
# mypy: disable-error-code="valid-type"

# State must be a flat TypedDict — never Pydantic BaseModel. Graph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only.
# Do NOT add credentials, secrets, or Pydantic models.

from typing import NotRequired, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """FIN-C2-004 Financial Document Summarization Agent state.

    All shared fields (user_input, input_context, status, session_id,
    node_history, error_log, hitl_*, formatted_output, etc.) are inherited
    from AgentState. Agent-specific fields below.

    Bare domain fields are wrapped in NotRequired so the TypedDict is valid
    at graph initialisation before nodes write values.
    """

    # Input processing
    input_text: NotRequired[str]
    chunked_segments: NotRequired[list]

    # Validated caller metadata (written by PreProcessNode from input_context)
    document_type: NotRequired[str]
    fiscal_period: NotRequired[str]
    materiality_threshold: NotRequired[float]
    focus_terms: NotRequired[list]

    # Summarization outputs
    summary_output: NotRequired[str]
    key_figures: NotRequired[str]
    risk_flags: NotRequired[str]
    action_items: NotRequired[str]

    # Error tracking
    error_message: Optional[str]
    error_code: Optional[str]
