"""Command-line entry point for drift-be-gone (drift)."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .analyze import build_graph
from .engine import RuleEngine, validate_manifest
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
    parser = argparse.ArgumentParser(prog="drift", description="Architecture gate + repo map.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="enforce the architecture manifest")
    check.add_argument("repo_root", help="repository root to scan")
    check.add_argument("--manifest", default=None, help="path to a custom manifest")
    check.add_argument("--json", action="store_true", help="emit violations as JSON")
    check.add_argument("--strict", action="store_true", help="fail on warnings too")
    mapper = subparsers.add_parser("map", help="emit the understanding artifact")
    mapper.add_argument("repo_root", help="repository root to map")
    mapper.add_argument("--manifest", default=None, help="path to a custom manifest")
    mapper.add_argument("--output", default=None, help="write the map JSON to a file")
    validate = subparsers.add_parser("validate", help="validate a manifest")
    validate.add_argument("--manifest", default=None, help="path to a custom manifest")
    return parser


def _run_check(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).expanduser()
    manifest = _discover_manifest(root, args.manifest)
    engine = RuleEngine(manifest)
    violations = engine.scan(root, strict=args.strict)
    if args.json:
        print(json.dumps([asdict(v) for v in violations], indent=2))
    elif not violations:
        print("No architecture violations found.")
    else:
        for v in violations:
            loc = f"{v.path}:{v.line}" if v.line is not None else v.path
            print(f"{loc}: [{v.rule_id}] {v.message}")
    return 1 if violations else 0


def _run_map(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).expanduser()
    manifest = _discover_manifest(root, args.manifest)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _run_check(args)
    if args.command == "map":
        return _run_map(args)
    return _run_validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
