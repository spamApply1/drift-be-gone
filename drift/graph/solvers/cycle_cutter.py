"""Graph-theoretic cycle resolution solver.

Uses Tarjan's strongly connected components algorithm and feedback-arc heuristics
to identify circular dependencies down to the symbol/call level and recommend
exact seams to break the cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model import Edge, Node, PropertyGraph


@dataclass
class CycleSeamCandidate:
    """A recommended cut or refactoring seam to break a cycle."""

    cut_edge: tuple[str, str]  # (source_id, target_id)
    edge_kind: str
    line: int | None
    strategy: str  # "extract_to_shared", "dependency_injection", "dynamic_import"
    description: str
    affected_symbols: list[str] = field(default_factory=list)
    impact_score: int = 1  # Lower is better (fewer dependents affected)


@dataclass
class CycleDiagnostic:
    """Diagnostic details for a circular dependency in the knowledge graph."""

    cycle_chain: list[str]
    scc_nodes: list[str]
    symbol_calls_involved: list[dict[str, Any]]
    seam_candidates: list[CycleSeamCandidate]


def _tarjan_scc(adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components algorithm."""
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in adjacency.get(node, []):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == node:
                    break
            sccs.append(scc)

    for node in sorted(adjacency):
        if node not in indices:
            strongconnect(node)

    return [s for s in sccs if len(s) > 1]


def _find_cycles_in_scc(scc_nodes: set[str], adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Find simple cycles within an SCC using DFS."""
    cycles: list[list[str]] = []
    visited: set[str] = set()

    for start_node in sorted(scc_nodes):
        stack: list[str] = [start_node]

        def dfs(curr: str) -> None:
            for neighbor in adjacency.get(curr, []):
                if neighbor not in scc_nodes:
                    continue
                if neighbor == start_node and len(stack) > 1:
                    cycles.append(list(stack) + [start_node])
                    return
                if neighbor not in stack and neighbor not in visited:
                    stack.append(neighbor)
                    dfs(neighbor)
                    stack.pop()

        dfs(start_node)
        visited.add(start_node)

    return cycles


def analyze_cycles(graph: PropertyGraph) -> list[CycleDiagnostic]:
    """Detect circular dependencies and compute symbol-level seam candidates."""
    # Build module-level adjacency
    modules = {n.id: n for n in graph.find_nodes(kind="module")}
    adj: dict[str, list[str]] = {mid: [] for mid in modules}

    for edge in graph.edges:
        if edge.kind == "IMPORTS" and edge.source in modules and edge.target in modules:
            if edge.target not in adj[edge.source]:
                adj[edge.source].append(edge.target)

    sccs = _tarjan_scc(adj)
    diagnostics: list[CycleDiagnostic] = []

    for scc in sccs:
        scc_set = set(scc)
        cycles = _find_cycles_in_scc(scc_set, adj)
        primary_cycle = cycles[0] if cycles else scc + [scc[0]]

        # Gather symbol-level call relationships across this SCC
        symbol_calls: list[dict[str, Any]] = []
        for edge in graph.edges:
            if edge.kind == "CALLS":
                src_node = graph.get_node(edge.source)
                tgt_node = graph.get_node(edge.target)
                src_mod = f"module:{src_node.path}" if src_node and src_node.path else None
                tgt_mod = f"module:{tgt_node.path}" if tgt_node and tgt_node.path else None
                if src_mod in scc_set and tgt_mod in scc_set and src_mod != tgt_mod:
                    symbol_calls.append(
                        {
                            "source_symbol": edge.source,
                            "target_symbol": edge.target,
                            "source_module": src_mod,
                            "target_module": tgt_mod,
                            "line": edge.line,
                        }
                    )

        # Compute seam candidates
        candidates: list[CycleSeamCandidate] = []
        # Check edges on the primary cycle
        for i in range(len(primary_cycle) - 1):
            src_m = primary_cycle[i]
            tgt_m = primary_cycle[i + 1]

            # Find matching call edges between these modules
            calls_between = [
                c for c in symbol_calls if c["source_module"] == src_m and c["target_module"] == tgt_m
            ]
            called_symbols = [c["target_symbol"] for c in calls_between]

            src_path = src_m.replace("module:", "")
            tgt_path = tgt_m.replace("module:", "")

            if calls_between:
                sym_names = [s.split(":")[-1] for s in called_symbols]
                candidates.append(
                    CycleSeamCandidate(
                        cut_edge=(src_m, tgt_m),
                        edge_kind="IMPORTS/CALLS",
                        line=calls_between[0].get("line"),
                        strategy="extract_to_shared",
                        description=(
                            f"Extract function(s) {sym_names} from '{tgt_path}' into a separate shared module "
                            f"(e.g., 'shared/{tgt_path.split('/')[-1]}'). Both '{src_path}' and '{tgt_path}' "
                            f"can import from shared without circular dependency."
                        ),
                        affected_symbols=called_symbols,
                        impact_score=len(calls_between),
                    )
                )
                candidates.append(
                    CycleSeamCandidate(
                        cut_edge=(src_m, tgt_m),
                        edge_kind="IMPORTS/CALLS",
                        line=calls_between[0].get("line"),
                        strategy="dependency_injection",
                        description=(
                            f"Inject {sym_names} as callable parameters into the caller in '{src_path}', "
                            f"removing the static module import."
                        ),
                        affected_symbols=called_symbols,
                        impact_score=len(calls_between) + 1,
                    )
                )
            else:
                candidates.append(
                    CycleSeamCandidate(
                        cut_edge=(src_m, tgt_m),
                        edge_kind="IMPORTS",
                        line=None,
                        strategy="dynamic_import",
                        description=(
                            f"Move import of '{tgt_path}' inside the specific calling function in '{src_path}' "
                            f"to defer loading."
                        ),
                        affected_symbols=[],
                        impact_score=3,
                    )
                )

        # Sort candidates by impact score
        candidates.sort(key=lambda c: c.impact_score)

        diagnostics.append(
            CycleDiagnostic(
                cycle_chain=primary_cycle,
                scc_nodes=scc,
                symbol_calls_involved=symbol_calls,
                seam_candidates=candidates,
            )
        )

    return diagnostics
