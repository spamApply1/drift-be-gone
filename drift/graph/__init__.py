"""Knowledge graph package for drift-be-gone.

Provides a unified semantic code graph across code, UI, workflows, and rules,
enabling automated graph-theoretic diagnostics and LLM-assisted architectural remediation.
"""

from .builder import build_knowledge_graph
from .model import Edge, Node, PropertyGraph
from .solvers import (
    AbstractionCluster,
    BlastRadiusReport,
    CycleDiagnostic,
    CycleSeamCandidate,
    ImplementationMatch,
    LayerViolationDiagnostic,
    SubgraphPacker,
    analyze_abstractions,
    analyze_cycles,
    compute_blast_radius,
    find_duplicate_implementations,
    trace_layer_violations,
)
from .store import discover_or_build_graph, load_graph, save_graph

__all__ = [
    "PropertyGraph",
    "Node",
    "Edge",
    "build_knowledge_graph",
    "save_graph",
    "load_graph",
    "discover_or_build_graph",
    "analyze_cycles",
    "ImplementationMatch",
    "find_duplicate_implementations",
    "CycleDiagnostic",
    "CycleSeamCandidate",
    "trace_layer_violations",
    "LayerViolationDiagnostic",
    "compute_blast_radius",
    "BlastRadiusReport",
    "analyze_abstractions",
    "AbstractionCluster",
    "SubgraphPacker",
]
