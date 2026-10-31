"""Semantic code property graph model for drift-be-gone.

Pure-Python, zero-dependency graph representation storing modules, symbols,
calls, UI elements, workflows, and rule violations.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator


@dataclass
class Node:
    """A node in the code knowledge graph."""

    id: str
    kind: str  # module, file, function, method, class, variable, ui_element, ui_action, workflow, rule, violation, layer
    name: str
    path: str | None = None
    line: int | None = None
    end_line: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "properties": self.properties,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=data["id"],
            kind=data["kind"],
            name=data["name"],
            path=data.get("path"),
            line=data.get("line"),
            end_line=data.get("end_line"),
            properties=data.get("properties", {}),
        )


@dataclass
class Edge:
    """A directed edge in the code knowledge graph."""

    source: str
    target: str
    kind: str  # IMPORTS, CALLS, DEFINES, INHERITS, DISPATCHES_TO, BINDS_UI, BELONGS_TO, VIOLATES, RESTRICTED_BY
    line: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "line": self.line,
            "properties": self.properties,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge:
        return cls(
            source=data["source"],
            target=data["target"],
            kind=data["kind"],
            line=data.get("line"),
            properties=data.get("properties", {}),
        )


class PropertyGraph:
    """In-memory property graph with multi-domain indexing and traversal methods."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._out_edges: dict[str, list[Edge]] = {}
        self._in_edges: dict[str, list[Edge]] = {}
        self._by_kind: dict[str, set[str]] = {}
        self._by_path: dict[str, set[str]] = {}

    def add_node(self, node: Node) -> Node:
        """Add or update a node in the graph."""
        old = self.nodes.get(node.id)
        if old is not None:
            # Remove old indices
            self._by_kind.get(old.kind, set()).discard(old.id)
            if old.path:
                self._by_path.get(old.path, set()).discard(old.id)

        self.nodes[node.id] = node
        self._by_kind.setdefault(node.kind, set()).add(node.id)
        if node.path:
            self._by_path.setdefault(node.path, set()).add(node.id)
        self._out_edges.setdefault(node.id, [])
        self._in_edges.setdefault(node.id, [])
        return node

    def add_edge(self, edge: Edge) -> Edge:
        """Add a directed edge between nodes."""
        self._out_edges.setdefault(edge.source, []).append(edge)
        self._in_edges.setdefault(edge.target, []).append(edge)
        self.edges.append(edge)
        return edge

    def get_node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def get_out_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        out = self._out_edges.get(node_id, [])
        if kind is None:
            return list(out)
        return [e for e in out if e.kind == kind]

    def get_in_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        incoming = self._in_edges.get(node_id, [])
        if kind is None:
            return list(incoming)
        return [e for e in incoming if e.kind == kind]

    def find_nodes(self, kind: str | None = None, path: str | None = None) -> list[Node]:
        """Query nodes by kind and/or path."""
        candidate_ids: set[str] | None = None
        if kind is not None:
            candidate_ids = set(self._by_kind.get(kind, set()))
        if path is not None:
            path_ids = self._by_path.get(path, set())
            candidate_ids = path_ids if candidate_ids is None else candidate_ids & path_ids

        if candidate_ids is None:
            return list(self.nodes.values())
        return [self.nodes[nid] for nid in sorted(candidate_ids) if nid in self.nodes]

    def neighbors(
        self,
        node_id: str,
        direction: str = "out",
        kind: str | None = None,
    ) -> list[Node]:
        """Return adjacent nodes according to direction ('out', 'in', 'both')."""
        result_ids: set[str] = set()
        if direction in ("out", "both"):
            for edge in self.get_out_edges(node_id, kind=kind):
                result_ids.add(edge.target)
        if direction in ("in", "both"):
            for edge in self.get_in_edges(node_id, kind=kind):
                result_ids.add(edge.source)
        return [self.nodes[nid] for nid in sorted(result_ids) if nid in self.nodes]

    def shortest_path(
        self,
        source_id: str,
        target_id: str,
        edge_kinds: tuple[str, ...] | None = None,
    ) -> list[str] | None:
        """Find the shortest directed path from source to target using BFS."""
        if source_id == target_id:
            return [source_id]
        if source_id not in self.nodes or target_id not in self.nodes:
            return None

        queue: deque[list[str]] = deque([[source_id]])
        visited: set[str] = {source_id}

        while queue:
            path = queue.popleft()
            curr = path[-1]
            out = self.get_out_edges(curr)
            if edge_kinds:
                out = [e for e in out if e.kind in edge_kinds]
            for edge in out:
                nxt = edge.target
                if nxt == target_id:
                    return path + [nxt]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None

    def all_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = 5,
        edge_kinds: tuple[str, ...] | None = None,
    ) -> list[list[str]]:
        """Find all directed paths between source and target up to max_depth."""
        results: list[list[str]] = []
        if source_id not in self.nodes or target_id not in self.nodes:
            return results

        def dfs(current: str, path: list[str], visited: set[str]) -> None:
            if current == target_id:
                results.append(list(path))
                return
            if len(path) > max_depth:
                return

            out = self.get_out_edges(current)
            if edge_kinds:
                out = [e for e in out if e.kind in edge_kinds]
            for edge in out:
                nxt = edge.target
                if nxt not in visited:
                    visited.add(nxt)
                    path.append(nxt)
                    dfs(nxt, path, visited)
                    path.pop()
                    visited.remove(nxt)

        dfs(source_id, [source_id], {source_id})
        return results

    def subgraph(self, node_ids: set[str]) -> PropertyGraph:
        """Extract an induced subgraph containing only specified nodes and edges between them."""
        sub = PropertyGraph()
        for nid in node_ids:
            node = self.nodes.get(nid)
            if node:
                sub.add_node(node)
        for edge in self.edges:
            if edge.source in node_ids and edge.target in node_ids:
                sub.add_edge(edge)
        return sub

    def k_hop_neighborhood(
        self,
        seed_ids: set[str],
        k: int = 1,
        direction: str = "both",
        edge_kinds: tuple[str, ...] | None = None,
    ) -> PropertyGraph:
        """Extract a k-hop neighborhood around seed nodes."""
        collected = set(seed_ids)
        current_layer = set(seed_ids)

        for _ in range(k):
            next_layer: set[str] = set()
            for nid in current_layer:
                if direction in ("out", "both"):
                    for edge in self.get_out_edges(nid):
                        if not edge_kinds or edge.kind in edge_kinds:
                            if edge.target not in collected:
                                next_layer.add(edge.target)
                if direction in ("in", "both"):
                    for edge in self.get_in_edges(nid):
                        if not edge_kinds or edge.kind in edge_kinds:
                            if edge.source not in collected:
                                next_layer.add(edge.source)
            collected.update(next_layer)
            current_layer = next_layer
            if not current_layer:
                break

        return self.subgraph(collected)

    def summary(self) -> dict[str, Any]:
        """Emit statistical overview of graph topology."""
        kinds_count: dict[str, int] = {}
        for node in self.nodes.values():
            kinds_count[node.kind] = kinds_count.get(node.kind, 0) + 1

        edges_count: dict[str, int] = {}
        for edge in self.edges:
            edges_count[edge.kind] = edges_count.get(edge.kind, 0) + 1

        in_deg: dict[str, int] = {nid: len(edges) for nid, edges in self._in_edges.items()}
        out_deg: dict[str, int] = {nid: len(edges) for nid, edges in self._out_edges.items()}

        top_in = sorted(in_deg.items(), key=lambda x: -x[1])[:10]
        top_out = sorted(out_deg.items(), key=lambda x: -x[1])[:10]

        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "node_kinds": kinds_count,
            "edge_kinds": edges_count,
            "top_in_degree": [{"id": k, "in_degree": v} for k, v in top_in],
            "top_out_degree": [{"id": k, "out_degree": v} for k, v in top_out],
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize property graph to dictionary."""
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PropertyGraph:
        """Deserialize property graph from dictionary."""
        graph = cls()
        for node_data in data.get("nodes", []):
            graph.add_node(Node.from_dict(node_data))
        for edge_data in data.get("edges", []):
            graph.add_edge(Edge.from_dict(edge_data))
        return graph

    def to_mermaid(self, max_nodes: int = 50) -> str:
        """Convert graph or subgraph to a Mermaid flowchart."""
        lines = ["flowchart TD"]
        selected_nodes = list(self.nodes.values())[:max_nodes]
        node_id_map: dict[str, str] = {}

        for idx, node in enumerate(selected_nodes):
            safe_id = f"n{idx}"
            node_id_map[node.id] = safe_id
            label = f"{node.name} [{node.kind}]"
            lines.append(f'    {safe_id}["{label}"]')

        for edge in self.edges:
            if edge.source in node_id_map and edge.target in node_id_map:
                s = node_id_map[edge.source]
                t = node_id_map[edge.target]
                lines.append(f'    {s} -- "{edge.kind}" --> {t}')

        return "\n".join(lines)
