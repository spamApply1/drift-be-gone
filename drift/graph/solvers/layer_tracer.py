"""Layer boundary path tracer and inversion-of-control resolver.

Traces direct and transitive paths that breach architectural layering rules
(e.g., lower layers importing upper layers or forbidden dependencies).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from ..model import PropertyGraph


@dataclass
class LayerViolationDiagnostic:
    """Diagnostic details for an architectural layer boundary violation."""

    source_module: str
    target_module: str
    source_layer: str
    target_layer: str
    paths: list[list[str]]
    remediation_suggestion: str


def _matches(path: str, patterns: list[str] | str) -> bool:
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(fnmatch(path, pattern) for pattern in patterns)


def trace_layer_violations(
    graph: PropertyGraph,
    layers: list[dict[str, Any]],
) -> list[LayerViolationDiagnostic]:
    """Identify layer violations and compute exact dependency paths."""
    rank: dict[str, int] = {}
    globs: dict[str, list[str]] = {}
    for idx, layer in enumerate(layers):
        name = layer.get("name", str(idx))
        rank[name] = idx
        globs[name] = layer.get("paths", [])

    def layer_of(path: str) -> str | None:
        for name, patterns in globs.items():
            if _matches(path, patterns):
                return name
        return None

    diagnostics: list[LayerViolationDiagnostic] = []

    # Check all module import edges
    for edge in graph.edges:
        if edge.kind != "IMPORTS":
            continue
        src_path = edge.source.replace("module:", "")
        tgt_path = edge.target.replace("module:", "")
        src_layer = layer_of(src_path)
        tgt_layer = layer_of(tgt_path)

        if src_layer is None or tgt_layer is None:
            continue

        # Violation: lower layer imports higher layer (rank[tgt] < rank[src])
        if rank[tgt_layer] < rank[src_layer]:
            # Trace all paths between these modules
            paths = graph.all_paths(edge.source, edge.target, max_depth=4, edge_kinds=("IMPORTS", "CALLS"))
            cleaned_paths = [[n.replace("module:", "").replace("fn:", "").replace("class:", "") for n in p] for p in paths]

            suggestion = (
                f"Invert dependency between '{src_path}' (layer: {src_layer}) and '{tgt_path}' (layer: {tgt_layer}). "
                f"Define an abstract interface or protocol in '{src_layer}' and implement it in '{tgt_layer}', "
                f"or pass handlers at application boot instead of directly importing."
            )

            diagnostics.append(
                LayerViolationDiagnostic(
                    source_module=src_path,
                    target_module=tgt_path,
                    source_layer=src_layer,
                    target_layer=tgt_layer,
                    paths=cleaned_paths or [[src_path, tgt_path]],
                    remediation_suggestion=suggestion,
                )
            )

    return diagnostics
