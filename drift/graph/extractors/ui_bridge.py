"""UI Bridge extractor connecting markup actions, classes, and handlers into the knowledge graph.

Bridges HTML/JSX/Vue/Svelte surfaces to script handlers (integrating slop-be-gone and design-be-gone concepts).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import Edge, Node, PropertyGraph

_DATA_ACTION = re.compile(r"""\bdata-action\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_ELEM_ID = re.compile(r"""\bid\s*=\s*['"]([A-Za-z0-9_-]+)['"]""", re.IGNORECASE)


class UIBridgeExtractor:
    """Extracts UI elements, actions, and dispatches to handler functions."""

    def __init__(self, root: Path, graph: PropertyGraph) -> None:
        self.root = root
        self.graph = graph

    def extract_file(self, path: Path) -> None:
        rel = path.relative_to(self.root).as_posix()
        ui_id = f"ui:{rel}"
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return

        self.graph.add_node(
            Node(
                id=ui_id,
                kind="ui_surface",
                name=path.name,
                path=rel,
                line=1,
                properties={"type": path.suffix.lower()},
            )
        )

        # Extract data-actions
        for match in _DATA_ACTION.finditer(content):
            action_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            action_id = f"action:{action_name}"

            self.graph.add_node(
                Node(
                    id=action_id,
                    kind="ui_action",
                    name=action_name,
                    path=rel,
                    line=line,
                )
            )
            self.graph.add_edge(Edge(source=ui_id, target=action_id, kind="BINDS_UI", line=line))

            # Attempt to link action to any function with matching name in graph
            matching_fns = [
                n for n in self.graph.find_nodes(kind="function")
                if n.name == action_name or n.name == f"handle_{action_name}" or n.name == f"on_{action_name}"
            ]
            for fn in matching_fns:
                self.graph.add_edge(
                    Edge(source=action_id, target=fn.id, kind="DISPATCHES_TO", properties={"confidence": "high"})
                )

        # Extract element IDs
        for match in _ELEM_ID.finditer(content):
            elem_id_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            elem_node_id = f"element:{rel}:{elem_id_name}"
            self.graph.add_node(
                Node(
                    id=elem_node_id,
                    kind="ui_element",
                    name=elem_id_name,
                    path=rel,
                    line=line,
                )
            )
            self.graph.add_edge(Edge(source=ui_id, target=elem_node_id, kind="CONTAINS", line=line))
