"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import FinC2004Agent

# Serialized size cap for the caller-metadata channel — enforced at the
# adapter so oversized payloads never reach the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024

app = FastAPI(title="Agent")


def _load_runtime_config() -> dict[str, Any]:
    """Load config/config.yaml — the runtime parameters (max_retry, timeout_s).

    The platform registry loads this file itself and passes it to the graph
    constructor; the standalone server must do the same, or the declared
    values silently never reach the graph.
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded if isinstance(loaded, dict) else {}


agent = FinC2004Agent(config=_load_runtime_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="fin-c2-004", agent_name="FIN-C2-004"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured caller metadata (document_type, fiscal_period,
    # materiality_threshold, focus_terms) — validated field-by-field in
    # PreProcessNode; the adapter only enforces the size cap.
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    context_size = len(json.dumps(req.input_context, ensure_ascii=False).encode("utf-8"))
    if context_size > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"input_context exceeds the {_MAX_INPUT_CONTEXT_BYTES}-byte limit",
        )

    with bound_secrets(agent._secrets_provider):
        # Trust promotion: callers that present a valid INVOKE_AUTH_TOKEN
        # Bearer credential are VERIFIED_EXTERNAL — the minimum trust required
        # by PreProcessNode. Unauthenticated requests remain ANONYMOUS.
        # When a higher-trust middleware (e.g. AgentGateway) already set
        # request.state.trust_level it takes precedence over the ANONYMOUS
        # default.
        _env_token = os.environ.get("INVOKE_AUTH_TOKEN", "")
        _auth_header = request.headers.get("Authorization", "")
        if _env_token and _auth_header == f"Bearer {_env_token}":
            trust = TrustLevel.VERIFIED_EXTERNAL
        else:
            trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # invoke() comes from the untyped framework package; its result is the
        # documented output mapping.
        return cast(dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=req.input_context))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "FinancialDocumentSummarizationAgent"}
