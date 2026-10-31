"""LLM Context Optimizer and Subgraph Packer.

Compresses complex graph topologies into targeted, token-efficient subgraphs,
Mermaid diagrams, and high-signal LLM remediation prompt payloads.
"""

from __future__ import annotations

import json
from typing import Any

from ..model import PropertyGraph
from .blast_radius import compute_blast_radius
from .cycle_cutter import analyze_cycles
from .duplicates import find_duplicate_implementations


class SubgraphPacker:
    """Produces token-optimized context blocks and LLM refactoring prompts."""

    def __init__(self, graph: PropertyGraph) -> None:
        self.graph = graph

    def pack_neighborhood(self, target_id_or_path: str, k: int = 1) -> dict[str, Any]:
        """Extract a k-hop subgraph around a target."""
        target_node = self.graph.get_node(target_id_or_path)
        if not target_node:
            target_node = self.graph.get_node(f"module:{target_id_or_path}")
        if not target_node:
            nodes = self.graph.find_nodes(path=target_id_or_path)
            if nodes:
                target_node = nodes[0]

        if not target_node:
            return {"error": f"Target '{target_id_or_path}' not found in knowledge graph."}

        sub = self.graph.k_hop_neighborhood({target_node.id}, k=k)
        blast = compute_blast_radius(self.graph, target_node.id)
        duplicate_candidates = [
            match.to_dict()
            for match in find_duplicate_implementations(self.graph, limit=30)
            if target_node.id in {match.left["id"], match.right["id"]}
            or (target_node.path and target_node.path in {match.left["path"], match.right["path"]})
        ][:10]

        return {
            "target": target_node.to_dict(),
            "subgraph_nodes": [n.to_dict() for n in sub.nodes.values()],
            "subgraph_edges": [e.to_dict() for e in sub.edges],
            "mermaid": sub.to_mermaid(),
            "blast_radius": {
                "risk_level": blast.risk_level if blast else "unknown",
                "upstream_count": blast.afferent_coupling if blast else 0,
                "downstream_count": blast.efferent_coupling if blast else 0,
                "impacted_files": blast.impacted_files if blast else [],
            },
            "similar_implementations": duplicate_candidates,
        }

    def generate_llm_remediation_prompt(self, target_id_or_path: str) -> str:
        """Construct a high-density, actionable refactoring prompt for an LLM."""
        packed = self.pack_neighborhood(target_id_or_path, k=1)
        if "error" in packed:
            return packed["error"]

        target = packed["target"]
        mermaid = packed["mermaid"]
        blast = packed["blast_radius"]
        similar_implementations = packed.get("similar_implementations", [])

        # Check for cycles involving this target
        cycle_diags = analyze_cycles(self.graph)
        relevant_cycles = [
            c for c in cycle_diags
            if any(target["id"] in n or target.get("path") in n for n in c.cycle_chain)
        ]

        prompt_lines = [
            "# Architectural Remediation Task for Coding Agent",
            "",
            f"**Target Entity**: `{target['id']}` (Kind: `{target['kind']}`, Path: `{target.get('path')}`)",
            f"**Blast Radius Risk**: `{blast['risk_level'].upper()}` ({blast['upstream_count']} incoming callers, {blast['downstream_count']} outgoing deps)",
            "",
            "## 1. Architectural Topology (Neighborhood Subgraph)",
            "```mermaid",
            mermaid,
            "```",
            "",
        ]

        if relevant_cycles:
            prompt_lines.append("## 2. Circular Dependency Diagnostics")
            for idx, c in enumerate(relevant_cycles, start=1):
                prompt_lines.append(f"### Cycle {idx}: {' -> '.join(c.cycle_chain)}")
                prompt_lines.append("**Recommended Seam Cuts**:")
                for seam in c.seam_candidates:
                    prompt_lines.append(f"- **Strategy (`{seam.strategy}`)**: {seam.description}")
            prompt_lines.append("")

        if similar_implementations:
            prompt_lines.append("## Similar Implementation Shapes to Review")
            prompt_lines.append(
                "These functions have similar normalized syntax and call patterns. "
                "Compare their responsibilities before consolidating; shape similarity alone does not prove equivalent behavior."
            )
            for match in similar_implementations:
                left, right = match["left"], match["right"]
                prompt_lines.append(
                    f"- `{left['path']}:{left['line']} {left['name']}` and "
                    f"`{right['path']}:{right['line']} {right['name']}` "
                    f"({match['similarity']:.0%}; {', '.join(match['common_patterns'][:5])})"
                )
            prompt_lines.append("")

        # Check for user workflows and duplicate workflow patterns involving this target
        target_path = target.get("path") or ""
        target_node_id = target.get("id") or ""
        wf_notes: list[str] = []

        participating_wf_ids = set()
        for edge in self.graph.edges:
            if edge.kind == "PARTICIPATES_IN_WORKFLOW":
                if edge.source in (target_node_id, f"module:{target_path}") or (target_path and target_path in edge.source):
                    participating_wf_ids.add(edge.target)

        for wf_id in sorted(participating_wf_ids):
            wf_node = self.graph.get_node(wf_id)
            wf_name = wf_node.name if wf_node else wf_id.replace("workflow:", "")
            for edge in self.graph.edges:
                if edge.kind == "DUPLICATES_WORKFLOW_PATTERN":
                    if edge.source == wf_id:
                        other_wf_id = edge.target
                        other_node = self.graph.get_node(other_wf_id)
                        other_name = other_node.name if other_node else other_wf_id.replace("workflow:", "")
                        other_file = edge.properties.get("source_a") or (other_node.properties.get("source_file") if other_node else "")
                        sim = edge.properties.get("similarity", 0.0)
                        wf_notes.append(
                            f"This module implements workflow `{wf_name}`, which has a {sim:.0%} behavioral step overlap with "
                            f"`{other_name}` in `{other_file}`. Refactor `{target_path}` to delegate to the `{other_name}` flow "
                            f"instead of maintaining a parallel state mutation sequence."
                        )
                    elif edge.target == wf_id:
                        other_wf_id = edge.source
                        other_node = self.graph.get_node(other_wf_id)
                        other_name = other_node.name if other_node else other_wf_id.replace("workflow:", "")
                        other_file = edge.properties.get("source_b") or (other_node.properties.get("source_file") if other_node else "")
                        sim = edge.properties.get("similarity", 0.0)
                        wf_notes.append(
                            f"This module implements workflow `{wf_name}`, which has a {sim:.0%} behavioral step overlap with "
                            f"`{other_name}` in `{other_file}`. Refactor `{target_path}` to delegate to the `{other_name}` flow "
                            f"instead of maintaining a parallel state mutation sequence."
                        )

        if wf_notes:
            prompt_lines.append("## Behavioral Workflow Governance")
            for note in sorted(set(wf_notes)):
                prompt_lines.append(f"- {note}")
            prompt_lines.append("")

        prompt_lines.extend(
            [
                "## 3. Invariant Constraints (Do Not Break)",
                f"- The following {len(blast['impacted_files'])} file(s) depend directly on this entity and must continue to compile and pass tests:",
            ]
        )
        for f in blast["impacted_files"][:10]:
            prompt_lines.append(f"  - `{f}`")
        if len(blast["impacted_files"]) > 10:
            prompt_lines.append(f"  - ... and {len(blast['impacted_files']) - 10} more files.")

        prompt_lines.extend(
            [
                "",
                "## 4. Remediation Instructions",
                "1. Apply the recommended refactoring seam above with minimal code modifications.",
                "2. Ensure all incoming callers in the invariant list continue to function without import errors.",
                "3. Re-run `drift check . --strict` to verify the architectural violation is completely resolved.",
            ]
        )

        return "\n".join(prompt_lines)
