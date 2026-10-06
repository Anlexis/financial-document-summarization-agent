"""AgentCore Platform v1.0"""

# Service layer: domain queries, external API wrappers, data aggregation.
# Must NOT contain business logic, routing, or credentials.
# Nodes call this; this calls shared/services/ for external integrations.
#
# Contract: FIN-C2-004 summarizes the document it is given — the pipeline is
# deterministic and self-contained, so no external domain service is called.
# `Service` is the seam where a real deployment wires a document store or a
# filings API client. `fetch()` therefore has a concrete no-op contract: it
# returns an empty result set rather than raising, so a caller that does reach
# it degrades gracefully instead of erroring.

from __future__ import annotations

from typing import Any


class Service:
    """Domain data-access seam for FIN-C2-004 (self-contained build).

    The shipped build summarizes the caller-supplied document directly; a real
    deployment wires a document store or filings API client here.
    """

    async def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return domain data for the query.

        No-op contract: no external document service is configured, so this
        returns an empty result set (`{"results": []}`). A real deployment
        replaces the body with the store / API call.
        """
        return {"results": []}
