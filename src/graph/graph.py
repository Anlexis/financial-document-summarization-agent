"""AgentCore Platform v1.0 — FIN-C2-004 FinC2004Agent"""

# Flat composition on the standard 5-node backbone:
#
#   START → initialize → pre_process → main → route → post_process → finalize → END
#
#   pre_process  = PreProcessNode  (input validation + injection/PII gate
#                                   + caller-metadata validation)
#   main         = SummarizeNode   (chunk + deterministic extractive summary)
#   post_process = PostProcessNode (output gate + report formatting)
#
# add_edges() is NOT overridden — the standard AgentBaseGraph routing is used.
# Domain complexity lives in the nodes.

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.summarize_node import SummarizeNode
from src.schemas.state import State


class FinC2004Agent(AgentBaseGraph):
    """FIN-C2-004 Financial Document Summarization Agent.

    Flat graph: validates the document and caller metadata, computes an
    extractive summary with aggregate key figures, then enforces the output
    gate and formats the report.
    """

    @property
    def name(self) -> str:
        return "FIN-C2-004"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # super() injects InitializeNode (sets schema_version, session_id, trust_level)
        # and FinalizeNode (builds response_metadata, total_time_ms).
        super().register_nodes()

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SummarizeNode()
        self._nodes["post_process"] = PostProcessNode()
