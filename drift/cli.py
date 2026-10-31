"""Command-line entry point for drift-be-gone (drift)."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .analyze import build_graph
from .engine import RuleEngine, validate_manifest
from .graph import (
    SubgraphPacker,
    analyze_cycles,
    build_knowledge_graph,
    compute_blast_radius,
    find_duplicate_implementations,
    save_graph,
    trace_layer_violations,
)
from .mapper import build_map

_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "data" / "default_manifest.json"


def _load_manifest(path: str | None) -> dict:
    manifest_path = Path(path).expanduser() if path else _DEFAULT_MANIFEST
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _discover_manifest(repo_root: Path, manifest_arg: str | None) -> dict:
    if manifest_arg:
        return _load_manifest(manifest_arg)
    for name in ("drift_manifest.json", ".drift/manifest.json", "drift.json", "architecture.json"):
        candidate = repo_root / name
        if candidate.is_file():
            return _load_manifest(str(candidate))
    return _load_manifest(None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drift", description="Architecture gate + knowledge graph + repo map.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. check
    check = subparsers.add_parser("check", help="enforce the architecture manifest")
    check.add_argument("repo_root", help="repository root to scan")
    check.add_argument("--manifest", default=None, help="path to a custom manifest")
    check.add_argument("--json", action="store_true", help="emit violations as JSON")
    check.add_argument("--strict", action="store_true", help="fail on warnings too")
    check.add_argument("--graph", action="store_true", help="include knowledge-graph remediation diagnostics")

    # 2. map
    mapper = subparsers.add_parser("map", help="emit the understanding artifact")
    mapper.add_argument("repo_root", help="repository root to map")
    mapper.add_argument("--manifest", default=None, help="path to a custom manifest")
    mapper.add_argument("--output", default=None, help="write the map JSON to a file")

    # 3. validate
    validate = subparsers.add_parser("validate", help="validate a manifest")
    validate.add_argument("--manifest", default=None, help="path to a custom manifest")

    # 4. graph
    graph_parser = subparsers.add_parser("graph", help="knowledge graph operations & LLM diagnostics")
    graph_sub = graph_parser.add_subparsers(dest="graph_command", required=True)

    # drift graph build
    g_build = graph_sub.add_parser("build", help="build the repository knowledge graph")
    g_build.add_argument("repo_root", help="repository root to analyze")
    g_build.add_argument("--manifest", default=None, help="manifest path for violation ingestion")
    g_build.add_argument("--output", default=None, help="output path (.drift/graph.json default)")
    g_build.add_argument("--json", action="store_true", help="print JSON graph to stdout")

    # drift graph explain
    g_explain = graph_sub.add_parser("explain", help="explain a violation or module using the graph")
    g_explain.add_argument("repo_root", help="repository root")
    g_explain.add_argument("target", help="target path, module id, or violation id")
    g_explain.add_argument("--manifest", default=None, help="manifest path")
    g_explain.add_argument("--json", action="store_true", help="output packed JSON")
    g_explain.add_argument("--mermaid", action="store_true", help="output Mermaid flowchart")

    # drift graph blast-radius
    g_blast = graph_sub.add_parser("blast-radius", help="compute change impact & coupling metrics")
    g_blast.add_argument("repo_root", help="repository root")
    g_blast.add_argument("target", help="target module or symbol to analyze")
    g_blast.add_argument("--json", action="store_true", help="output JSON report")

    # drift graph duplicates
    g_duplicates = graph_sub.add_parser("duplicates", help="find similar implementation shapes across modules")
    g_duplicates.add_argument("repo_root", help="repository root")
    g_duplicates.add_argument("--min-similarity", type=float, default=0.86, help="minimum shape similarity from 0 to 1")
    g_duplicates.add_argument("--include-tests", action="store_true", help="include test paths in the scan")
    g_duplicates.add_argument("--json", action="store_true", help="output JSON matches")

    # drift graph prompt
    g_prompt = graph_sub.add_parser("prompt", help="generate an LLM refactoring prompt for an issue")
    g_prompt.add_argument("repo_root", help="repository root")
    g_prompt.add_argument("target", help="target module, symbol, or violation")
    g_prompt.add_argument("--manifest", default=None, help="manifest path")

    return parser


def _run_check(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).expanduser()
    manifest = _discover_manifest(root, args.manifest)
    engine = RuleEngine(manifest)
    violations = engine.scan(root, strict=args.strict)

    graph = None
    if getattr(args, "graph", False) and violations:
        graph = build_knowledge_graph(root, manifest=manifest, include_violations=True)

    if args.json:
        payload = [asdict(v) for v in violations]
        if graph:
            cycle_diags = analyze_cycles(graph)
            if cycle_diags:
                payload = {
                    "violations": payload,
                    "cycle_diagnostics": [
                        {
                            "cycle": c.cycle_chain,
                            "seam_candidates": [asdict(s) for s in c.seam_candidates],
                        }
                        for c in cycle_diags
                    ],
                }
        print(json.dumps(payload, indent=2))
    elif not violations:
        print("No architecture violations found.")
    else:
        for v in violations:
            loc = f"{v.path}:{v.line}" if v.line is not None else v.path
            print(f"{loc}: [{v.rule_id}] {v.message}")

        if graph:
            cycle_diags = analyze_cycles(graph)
            if cycle_diags:
                print("\n🧠 Knowledge Graph Remediation Advice:")
                for c in cycle_diags:
                    print(f"  Cycle: {' -> '.join(c.cycle_chain)}")
                    if c.seam_candidates:
                        top = c.seam_candidates[0]
                        print(f"  Recommended Seam ({top.strategy}): {top.description}")

    return 1 if violations else 0


def _run_map(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).expanduser()
    manifest = _discover_manifest(root, args.manifest)
    problems = validate_manifest(manifest)
    if problems:
        for problem in problems:
            print(problem)
        return 1
    artifact = build_map(root, build_graph(root), manifest=manifest)
    text = json.dumps(artifact, indent=2)
    if args.output:
        Path(args.output).expanduser().write_text(text + "\n", encoding="utf-8")
        print(f"wrote map to {args.output}")
    else:
        print(text)
    return 0


def _run_validate(args: argparse.Namespace) -> int:
    problems = validate_manifest(_load_manifest(args.manifest))
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print("Manifest is valid.")
    return 0


def _run_graph(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).expanduser()
    manifest = _discover_manifest(root, getattr(args, "manifest", None))
    gcmd = args.graph_command

    if gcmd == "build":
        graph = build_knowledge_graph(root, manifest=manifest)
        out_path = args.output or (root / ".drift" / "graph.json")
        save_graph(graph, out_path)
        if args.json:
            print(json.dumps(graph.to_dict(), indent=2))
        else:
            summary = graph.summary()
            print(f"Built knowledge graph with {summary['total_nodes']} nodes, {summary['total_edges']} edges.")
            print(f"Saved graph to {out_path}")
        return 0

    if gcmd == "explain":
        graph = build_knowledge_graph(root, manifest=manifest)
        packer = SubgraphPacker(graph)
        packed = packer.pack_neighborhood(args.target)
        if args.json:
            print(json.dumps(packed, indent=2))
        elif args.mermaid:
            print(packed.get("mermaid", ""))
        else:
            target = packed.get("target", {})
            blast = packed.get("blast_radius", {})
            print(f"Target: {target.get('id')} ({target.get('kind')})")
            print(f"Risk Level: {blast.get('risk_level', 'unknown').upper()}")
            print(f"Upstream Callers: {blast.get('upstream_count')}, Outgoing Deps: {blast.get('downstream_count')}")
            print("\nMermaid Flowchart:")
            print(packed.get("mermaid", ""))
        return 0

    if gcmd == "blast-radius":
        graph = build_knowledge_graph(root, manifest=manifest)
        report = compute_blast_radius(graph, args.target)
        if not report:
            print(f"Target '{args.target}' not found in graph.")
            return 1
        if args.json:
            print(json.dumps(asdict(report), indent=2))
        else:
            print(f"Blast Radius Report for: {report.target_id}")
            print(f"Risk Level: {report.risk_level.upper()}")
            print(f"Afferent Coupling (Ca - Incoming): {report.afferent_coupling}")
            print(f"Efferent Coupling (Ce - Outgoing): {report.efferent_coupling}")
            print(f"Instability Metric (Ce / (Ca + Ce)): {report.instability}")
            print(f"Impacted Files ({len(report.impacted_files)}):")
            for f in report.impacted_files:
                print(f"  - {f}")
        return 0

    if gcmd == "duplicates":
        if not 0 < args.min_similarity <= 1:
            print("--min-similarity must be greater than 0 and at most 1")
            return 2
        graph = build_knowledge_graph(root, manifest=manifest, include_violations=False)
        ignored = [] if args.include_tests else ["tests/**", "**/*test*", "**/*_test.py", "**/*.test.*"]
        matches = find_duplicate_implementations(
            graph,
            min_similarity=args.min_similarity,
            ignore_paths=ignored,
        )
        if args.json:
            print(json.dumps([match.to_dict() for match in matches], indent=2))
        elif not matches:
            print("No likely duplicate implementation shapes found.")
        else:
            print(f"Found {len(matches)} possible duplicate implementation pair(s):")
            for match in matches:
                left, right = match.left, match.right
                patterns = ", ".join(match.common_patterns[:5]) or "matching control and operator shape"
                print(
                    f"{left['path']}:{left['line']} {left['name']} <-> "
                    f"{right['path']}:{right['line']} {right['name']} "
                    f"({match.similarity:.0%}; {patterns})"
                )
        return 0

    if gcmd == "prompt":
        graph = build_knowledge_graph(root, manifest=manifest)
        packer = SubgraphPacker(graph)
        prompt = packer.generate_llm_remediation_prompt(args.target)
        print(prompt)
        return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _run_check(args)
    if args.command == "map":
        return _run_map(args)
    if args.command == "validate":
        return _run_validate(args)
    if args.command == "graph":
        return _run_graph(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
