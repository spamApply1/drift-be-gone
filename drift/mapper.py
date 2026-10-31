"""Understanding map: emit a JSON artifact of what exists and what to touch.

This replaces the idud "bridges" artifact with a pure-Python equivalent that a
coding agent (or the Qaymark harness) can read to learn the repo's structure,
dependency graph, notable files, and inferred domains.
"""

from __future__ import annotations

from pathlib import Path

from .analyze import Graph, build_graph
from .engine import _normalize_abstractions


def _notable_files(graph: Graph, limit: int = 15) -> list[str]:
    in_degree = graph.in_degree()
    ranked = sorted(graph.modules, key=lambda path: (-in_degree.get(path, 0), path))
    return ranked[:limit]


def _symbol_index(graph: Graph, limit: int = 40) -> list[dict]:
    index: list[dict] = []
    for path in sorted(graph.modules):
        module = graph.modules[path]
        if module.functions or module.classes:
            index.append(
                {
                    "path": path,
                    "functions": module.functions[:20],
                    "classes": module.classes[:20],
                }
            )
        if len(index) >= limit:
            break
    return index


def _brief(graph: Graph) -> str:
    domains = graph.domains()
    domain_text = ", ".join(domains) if domains else "a flat layout"
    return (
        f"{len(graph.modules)} modules across {domain_text}; "
        f"{len(graph.edges)} internal dependency edges."
    )


def _extract_abstractions(manifest: dict | None) -> list[dict]:
    if not isinstance(manifest, dict):
        return []
    items: list[dict] = []
    rules = manifest.get("rules", [])
    if not isinstance(rules, list):
        return []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if r.get("type") != "canonical-abstractions" or not r.get("enabled", True):
            continue
        items.extend(_normalize_abstractions(r))
    return [
        {
            "name": a.get("name") or a.get("id") or "unnamed",
            "canonical": a.get("canonical"),
            "allow": a.get("allow") or [],
            "description": a.get("description", ""),
            "restricted_imports": a.get("restricted_imports") or [],
            "restricted_primitives": a.get("restricted_primitives") or [],
            "restricted_symbols": a.get("restricted_symbols") or [],
            "require_canonical_exists": a.get("require_canonical_exists", False),
        }
        for a in items
    ]


def _extract_workflows(root: Path) -> list[dict]:
    try:
        from .synthetic import SyntheticWorkflowGovernance

        gov = SyntheticWorkflowGovernance(
            root_dir=root,
            require_synthetic=False,
            detect_duplicate_patterns=False,
        )
        _, patterns = gov.check_repository()
    except Exception:
        return []

    workflows_by_id: dict[str, dict] = {}
    for p in patterns:
        if p.id not in workflows_by_id:
            workflows_by_id[p.id] = {
                "id": p.id,
                "user_intent": p.user_intent,
                "trigger": p.trigger,
                "steps": p.steps,
                "outcome": p.outcome,
                "participating_files": [p.source_file],
            }
        else:
            files = workflows_by_id[p.id]["participating_files"]
            if p.source_file not in files:
                files.append(p.source_file)
                files.sort()

    return [workflows_by_id[k] for k in sorted(workflows_by_id.keys())]


def build_map(
    root: Path,
    graph: Graph | None = None,
    manifest: dict | None = None,
) -> dict:
    """Return a JSON-friendly understanding artifact for *root*."""

    graph = graph or build_graph(root)
    notable = _notable_files(graph)
    data = {
        "summary": f"Understanding map for {root.name}",
        "synthetic_brief": _brief(graph),
        "inferred_domains": graph.domains(),
        "notable_files": notable,
        "graph_nodes": sorted(graph.modules),
        "graph_edges": [list(edge) for edge in graph.edges],
        "symbol_index": _symbol_index(graph),
    }
    abstractions = _extract_abstractions(manifest)
    if abstractions:
        data["canonical_abstractions"] = abstractions
    workflows = _extract_workflows(root)
    if workflows:
        data["canonical_workflows"] = workflows
    return data
