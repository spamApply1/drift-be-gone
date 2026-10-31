"""Canonical abstraction synthesis solver.

Analyzes restricted primitive usage and duplicate implementations across the graph,
generating recommendations and boilerplate to consolidate into declared canonical modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model import PropertyGraph


@dataclass
class AbstractionCluster:
    """A cluster of scattered primitive calls that should be consolidated."""

    canonical_path: str
    restricted_item: str
    violating_locations: list[dict[str, Any]] = field(default_factory=list)
    suggested_wrapper_interface: str = ""
    remediation_steps: list[str] = field(default_factory=list)


def analyze_abstractions(graph: PropertyGraph) -> list[AbstractionCluster]:
    """Find all scattered primitive usages and generate canonical consolidation recipes."""
    # Look for violation nodes with canonical abstraction properties
    clusters_by_canonical: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for v_node in graph.find_nodes(kind="violation"):
        canonical = v_node.properties.get("canonical")
        restriction = v_node.properties.get("restriction")
        if canonical and restriction:
            key = (canonical, restriction)
            clusters_by_canonical.setdefault(key, []).append(
                {
                    "path": v_node.path,
                    "line": v_node.line,
                    "message": v_node.properties.get("message"),
                }
            )

    results: list[AbstractionCluster] = []
    for (canonical, restriction), locations in sorted(clusters_by_canonical.items()):
        clean_name = restriction.replace(".", "_").replace("-", "_").lower()
        wrapper_code = (
            f"# Recommended canonical interface in {canonical}\n"
            f"class {clean_name.capitalize()}Client:\n"
            f"    def __init__(self, *args, **kwargs):\n"
            f"        # Encapsulate {restriction} here\n"
            f"        pass\n\n"
            f"    def execute(self, payload: dict) -> Any:\n"
            f"        raise NotImplementedError\n"
        )

        steps = [
            f"1. Implement {clean_name.capitalize()}Client in '{canonical}'.",
            f"2. Replace direct '{restriction}' imports in {len(locations)} violating file(s) with '{canonical}'.",
            f"3. Run 'drift check . --strict' to ensure no leakages remain outside the canonical boundary.",
        ]

        results.append(
            AbstractionCluster(
                canonical_path=canonical,
                restricted_item=restriction,
                violating_locations=locations,
                suggested_wrapper_interface=wrapper_code,
                remediation_steps=steps,
            )
        )

    return results
