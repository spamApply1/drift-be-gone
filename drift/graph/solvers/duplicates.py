"""Name-insensitive implementation-shape matching for review candidates."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from fnmatch import fnmatch
from itertools import combinations
from typing import Any

from ..model import Node, PropertyGraph


@dataclass(frozen=True)
class ImplementationMatch:
    """Two functions with a similar normalized implementation shape."""

    left: dict[str, Any]
    right: dict[str, Any]
    similarity: float
    shape_similarity: float
    call_similarity: float
    common_patterns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _location(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "path": node.path,
        "line": node.line,
        "language": node.properties.get("language", "unknown"),
    }


def _call_features(graph: PropertyGraph, node: Node) -> set[str]:
    calls = set(node.properties.get("intent_calls", []))
    for edge in graph.get_out_edges(node.id, kind="CALLS"):
        target = graph.get_node(edge.target)
        if target:
            calls.add(f"graph-call:{target.kind}:{target.name.rsplit('.', 1)[-1]}")
    return calls


def _similarity(graph: PropertyGraph, left: Node, right: Node) -> tuple[float, float, float]:
    left_shape = left.properties["intent_shape"]
    right_shape = right.properties["intent_shape"]
    shape_score = SequenceMatcher(None, left_shape, right_shape, autojunk=False).ratio()

    left_calls = _call_features(graph, left)
    right_calls = _call_features(graph, right)
    if not left_calls and not right_calls:
        call_score = 1.0
    elif left_calls and right_calls:
        call_score = len(left_calls & right_calls) / len(left_calls | right_calls)
    else:
        call_score = 0.0
    return shape_score * 0.85 + call_score * 0.15, shape_score, call_score


def _candidate_pairs(nodes: list[Node]) -> set[tuple[str, str]]:
    signatures: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    postings: dict[tuple[str, str], list[str]] = defaultdict(list)
    frequencies: Counter[tuple[str, str]] = Counter()
    for node in nodes:
        language = node.properties.get("language", "unknown")
        shape = node.properties["intent_shape"]
        signatures[(language, tuple(shape))].append(node.id)
        for feature in set(shape):
            key = (language, feature)
            postings[key].append(node.id)
            frequencies[key] += 1

    pairs: set[tuple[str, str]] = set()
    for ids in signatures.values():
        pairs.update(combinations(ids, 2))
    max_frequency = max(3, len(nodes) // 5)
    for key, ids in postings.items():
        if frequencies[key] <= max_frequency:
            pairs.update(combinations(ids, 2))
    return pairs


def _match_candidate(
    graph: PropertyGraph,
    left: Node,
    right: Node,
    min_similarity: float,
) -> ImplementationMatch | None:
    if left.path == right.path or left.properties.get("language") != right.properties.get("language"):
        return None
    common = set(left.properties["intent_shape"]) & set(right.properties["intent_shape"])
    if len(common) < 3:
        return None
    score, shape_score, call_score = _similarity(graph, left, right)
    if score < min_similarity:
        return None
    patterns = sorted(common - {"name", "attribute", "call", "identifier", "group"})
    return ImplementationMatch(
        left=_location(left),
        right=_location(right),
        similarity=round(score, 3),
        shape_similarity=round(shape_score, 3),
        call_similarity=round(call_score, 3),
        common_patterns=patterns[:12],
    )


def find_duplicate_implementations(
    graph: PropertyGraph,
    min_similarity: float = 0.86,
    min_features: int = 8,
    ignore_paths: list[str] | None = None,
    limit: int = 100,
) -> list[ImplementationMatch]:
    """Return cross-module shape matches, ranked as advisory review candidates.

    Function and method names, local names, and literal values are normalized by
    the extractors. Candidate generation uses rare shared shape features before
    the more expensive sequence comparison, keeping large repositories bounded.
    """
    if (
        isinstance(min_similarity, bool)
        or not isinstance(min_similarity, (int, float))
        or not 0 < min_similarity <= 1
    ):
        raise ValueError("min_similarity must be greater than 0 and at most 1")
    if isinstance(min_features, bool) or not isinstance(min_features, int) or min_features < 1:
        raise ValueError("min_features must be a positive integer")
    ignored = ignore_paths or []
    nodes = [
        node
        for node in graph.find_nodes()
        if node.kind in {"function", "method"}
        and node.path
        and not any(fnmatch(node.path, pattern) for pattern in ignored)
        and len(node.properties.get("intent_shape", [])) >= min_features
    ]
    nodes.sort(key=lambda node: node.id)
    by_id = {node.id: node for node in nodes}
    matches: list[ImplementationMatch] = []
    for left_id, right_id in sorted(_candidate_pairs(nodes)):
        match = _match_candidate(graph, by_id[left_id], by_id[right_id], min_similarity)
        if match:
            matches.append(match)

    matches.sort(key=lambda match: (-match.similarity, match.left["path"], match.right["path"]))
    return matches[: max(0, limit)]
