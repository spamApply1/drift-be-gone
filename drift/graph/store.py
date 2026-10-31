"""Persistence layer for drift knowledge graphs.

Supports saving/loading graphs as JSON and caching graph artifacts under .drift/.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import PropertyGraph


def save_graph(graph: PropertyGraph, output_path: str | Path) -> None:
    """Save graph as a pretty-printed JSON file."""
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = graph.to_dict()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_graph(input_path: str | Path) -> PropertyGraph:
    """Load graph from a JSON file."""
    path = Path(input_path).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    return PropertyGraph.from_dict(data)


def discover_or_build_graph(
    repo_root: Path,
    graph_path: str | Path | None = None,
) -> PropertyGraph | None:
    """Discover existing graph JSON or return None if not present."""
    if graph_path:
        p = Path(graph_path).expanduser()
        if p.is_file():
            return load_graph(p)
    default_loc = repo_root / ".drift" / "graph.json"
    if default_loc.is_file():
        return load_graph(default_loc)
    return None
