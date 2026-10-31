"""Blast-radius and change impact analyzer.

Computes transitive upstream (afferent) and downstream (efferent) impact closures
for any module or code symbol, helping an LLM anticipate the side-effects of a refactor.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..model import PropertyGraph


@dataclass
class BlastRadiusReport:
    """Summary of the change impact for a target node."""

    target_id: str
    target_kind: str
    target_path: str | None
    upstream_dependents: list[str] = field(default_factory=list)  # Callers / importers (who breaks if target changes)
    downstream_dependencies: list[str] = field(default_factory=list)  # Dependencies (what target relies on)
    afferent_coupling: int = 0  # Ca (incoming)
    efferent_coupling: int = 0  # Ce (outgoing)
    instability: float = 0.0  # Ce / (Ca + Ce)
    impacted_files: list[str] = field(default_factory=list)
    risk_level: str = "low"  # low, medium, high, critical


def compute_blast_radius(graph: PropertyGraph, target_id_or_path: str) -> BlastRadiusReport | None:
    """Compute transitive impact graph for a module, file, or symbol."""
    target_node = graph.get_node(target_id_or_path)
    if not target_node:
        # Check by module:path
        target_node = graph.get_node(f"module:{target_id_or_path}")
    if not target_node:
        # Check by path
        nodes_at_path = graph.find_nodes(path=target_id_or_path)
        if nodes_at_path:
            target_node = nodes_at_path[0]

    if not target_node:
        return None

    # 1. Transitive upstream (who depends on target)
    upstream: set[str] = set()
    queue: deque[str] = deque([target_node.id])
    while queue:
        curr = queue.popleft()
        for edge in graph.get_in_edges(curr):
            if edge.kind in ("IMPORTS", "CALLS", "INHERITS", "BINDS_UI"):
                if edge.source not in upstream and edge.source != target_node.id:
                    upstream.add(edge.source)
                    queue.append(edge.source)

    # 2. Transitive downstream (what target depends on)
    downstream: set[str] = set()
    queue = deque([target_node.id])
    while queue:
        curr = queue.popleft()
        for edge in graph.get_out_edges(curr):
            if edge.kind in ("IMPORTS", "CALLS", "INHERITS"):
                if edge.target not in downstream and edge.target != target_node.id:
                    downstream.add(edge.target)
                    queue.append(edge.target)

    ca = len(upstream)
    ce = len(downstream)
    denom = ca + ce
    instability = round(ce / denom, 3) if denom > 0 else 0.0

    # Impacted files
    impacted_files_set: set[str] = set()
    for nid in upstream:
        n = graph.get_node(nid)
        if n and n.path:
            impacted_files_set.add(n.path)

    # Risk level determination
    if ca >= 15:
        risk = "critical"
    elif ca >= 8:
        risk = "high"
    elif ca >= 3:
        risk = "medium"
    else:
        risk = "low"

    return BlastRadiusReport(
        target_id=target_node.id,
        target_kind=target_node.kind,
        target_path=target_node.path,
        upstream_dependents=sorted(upstream),
        downstream_dependencies=sorted(downstream),
        afferent_coupling=ca,
        efferent_coupling=ce,
        instability=instability,
        impacted_files=sorted(impacted_files_set),
        risk_level=risk,
    )
