"""Violation mapping extractor for the knowledge graph.

Integrates be-gone violations directly into the graph ontology so that rules,
violations, and offending code nodes/edges form a unified semantic topology.
"""

from __future__ import annotations

from typing import Any

from ..model import Edge, Node, PropertyGraph


class ViolationExtractor:
    """Ingests violations from rule engines and links them to code graph entities."""

    def __init__(self, graph: PropertyGraph) -> None:
        self.graph = graph

    def ingest_violations(self, violations: list[Any]) -> None:
        for v in violations:
            rule_id = getattr(v, "rule_id", "unknown-rule")
            path = getattr(v, "path", ".")
            line = getattr(v, "line", None)
            message = getattr(v, "message", "")
            severity = getattr(v, "severity", "error")

            # Create rule node if missing
            rule_node_id = f"rule:{rule_id}"
            if not self.graph.get_node(rule_node_id):
                self.graph.add_node(
                    Node(
                        id=rule_node_id,
                        kind="rule",
                        name=rule_id,
                        properties={"severity": severity},
                    )
                )

            # Create violation node
            v_id = f"violation:{rule_id}:{path}:{line or 0}"
            self.graph.add_node(
                Node(
                    id=v_id,
                    kind="violation",
                    name=f"[{rule_id}] {path}",
                    path=path,
                    line=line,
                    properties={
                        "rule_id": rule_id,
                        "message": message,
                        "severity": severity,
                        "canonical": getattr(v, "canonical", None),
                        "restriction": getattr(v, "restriction", None),
                    },
                )
            )

            # Edge to Rule
            self.graph.add_edge(Edge(source=v_id, target=rule_node_id, kind="ENFORCED_BY"))

            # Connect violation to target code node (module, function, or class)
            target_node = None
            if line is not None:
                # Look for matching function/method/class at that line
                candidates = [
                    n for n in self.graph.find_nodes(path=path)
                    if n.line is not None and n.line <= line and (n.end_line is None or n.end_line >= line)
                ]
                if candidates:
                    # Pick most specific (narrowest line span)
                    candidates.sort(key=lambda c: (c.end_line or 999999) - (c.line or 0))
                    target_node = candidates[0]

            if not target_node:
                target_node = self.graph.get_node(f"module:{path}")

            if target_node:
                self.graph.add_edge(
                    Edge(
                        source=v_id,
                        target=target_node.id,
                        kind="VIOLATES",
                        line=line,
                        properties={"message": message},
                    )
                )
