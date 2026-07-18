"""Architecture rule engine: enforce declared structure over the module graph."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .analyze import Graph, build_graph

RULE_TYPES = frozenset(
    {
        "no-cycles",
        "layering",
        "forbidden-deps",
        "orphan-modules",
        "entrypoints-exist",
    }
)


@dataclass
class Violation:
    rule_id: str
    path: str
    line: int | None
    message: str
    severity: str = "error"


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def _find_cycles(graph: Graph) -> list[list[str]]:
    adjacency: dict[str, list[str]] = {node: [] for node in graph.modules}
    for src, dst in graph.edges:
        adjacency[src].append(dst)
    cycles: list[list[str]] = []
    color: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = 1
        stack.append(node)
        for neighbour in adjacency.get(node, []):
            state = color.get(neighbour, 0)
            if state == 0:
                visit(neighbour, stack)
            elif state == 1 and neighbour in stack:
                cycles.append(stack[stack.index(neighbour):] + [neighbour])
        stack.pop()
        color[node] = 2

    for node in graph.modules:
        if color.get(node, 0) == 0:
            visit(node, [])
    return cycles


def _no_cycles(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "no-cycles")
    out: list[Violation] = []
    seen: set[frozenset[str]] = set()
    for cycle in _find_cycles(graph):
        key = frozenset(cycle)
        if key in seen:
            continue
        seen.add(key)
        chain = " -> ".join(cycle)
        out.append(Violation(rule_id, cycle[0], None, f"circular dependency: {chain}"))
    return out


def _layering(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "layering")
    layers: list[dict] = rule.get("layers", [])
    rank = {}
    globs = {}
    for index, layer in enumerate(layers):
        name = layer.get("name", str(index))
        rank[name] = index
        globs[name] = layer.get("paths", [])

    def layer_of(path: str) -> str | None:
        for name, patterns in globs.items():
            if _matches(path, patterns):
                return name
        return None

    out: list[Violation] = []
    for src, dst in graph.edges:
        src_layer = layer_of(src)
        dst_layer = layer_of(dst)
        if src_layer is None or dst_layer is None:
            continue
        if rank[dst_layer] < rank[src_layer]:
            message = f"layer '{src_layer}' must not import lower layer '{dst_layer}' ({dst})"
            out.append(Violation(rule_id, src, None, message))
    return out


def _forbidden_deps(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "forbidden-deps")
    pairs: list[dict] = rule.get("pairs", [])
    out: list[Violation] = []
    for src, dst in graph.edges:
        for pair in pairs:
            if _matches(src, pair.get("from", [])) and _matches(dst, pair.get("to", [])):
                out.append(Violation(rule_id, src, None, f"forbidden dependency on {dst}"))
    return out


def _orphan_modules(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "orphan-modules")
    allow = rule.get("allow", ["**/__init__.py", "**/main.py", "**/cli.py", "**/*.test.*"])
    incoming = {node: 0 for node in graph.modules}
    for _, dst in graph.edges:
        incoming[dst] += 1
    out: list[Violation] = []
    for path, count in sorted(incoming.items()):
        if count == 0 and not _matches(path, allow):
            out.append(Violation(rule_id, path, None, "module is never imported (possible orphan)"))
    return out


def _entrypoints_exist(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "entrypoints-exist")
    out: list[Violation] = []
    for entry in rule.get("entrypoints", []):
        if not (root / entry).exists():
            out.append(Violation(rule_id, entry, None, "declared entrypoint is missing"))
    return out


_RULES = {
    "no-cycles": _no_cycles,
    "layering": _layering,
    "forbidden-deps": _forbidden_deps,
    "orphan-modules": _orphan_modules,
    "entrypoints-exist": _entrypoints_exist,
}


class RuleEngine:
    def __init__(self, manifest: dict):
        self.rules = [r for r in manifest.get("rules", []) if r.get("enabled", True)]

    def scan(self, root: Path, strict: bool = False, graph: Graph | None = None) -> list[Violation]:
        graph = graph or build_graph(root)
        violations: list[Violation] = []
        for rule in self.rules:
            handler = _RULES.get(rule.get("type"))
            if handler is None:
                continue
            found = handler(rule, graph, root)
            severity = rule.get("severity", "error")
            for violation in found:
                violation.severity = severity
            violations.extend(found)
        if strict:
            return violations
        return [v for v in violations if v.severity == "error"]


def validate_manifest(manifest: dict) -> list[str]:
    problems: list[str] = []
    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["manifest must define a non-empty 'rules' array"]
    for index, rule in enumerate(rules):
        label = rule.get("id", f"rule[{index}]")
        if rule.get("type") not in RULE_TYPES:
            problems.append(f"{label}: unknown rule type {rule.get('type')!r}")
        if rule.get("severity", "error") not in {"error", "warning"}:
            problems.append(f"{label}: severity must be error or warning")
    return problems
