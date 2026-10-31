"""Knowledge graph orchestrator and builder.

Discovers repository files, dispatches extractors across Python, JS/TS, UI,
and workflow domains, and wires rule engine violations into the graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .extractors import (
    JSTSExtractor,
    PythonASTExtractor,
    UIBridgeExtractor,
    ViolationExtractor,
    WorkflowExtractor,
)
from .model import PropertyGraph

_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        ".pytest_cache",
    }
)
_PY = {".py"}
_JS = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
_UI = {".html", ".htm", ".vue", ".svelte"}


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        files.append(path)
    return sorted(files)


def build_knowledge_graph(
    root: Path,
    manifest: dict[str, Any] | None = None,
    include_violations: bool = True,
) -> PropertyGraph:
    """Construct a full semantic property graph of the repository."""
    graph = PropertyGraph()
    py_extractor = PythonASTExtractor(root, graph)
    js_extractor = JSTSExtractor(root, graph)
    ui_extractor = UIBridgeExtractor(root, graph)
    wf_extractor = WorkflowExtractor(root, graph)

    all_files = _iter_files(root)

    # 1. Pre-register module paths for symbol resolution
    for path in all_files:
        rel = path.relative_to(root).as_posix()
        ext = path.suffix.lower()
        if ext in _PY:
            py_extractor.register_module(rel)
        elif ext in _JS:
            js_extractor.register_module(rel)

    # 2. Extract code AST and dependencies
    for path in all_files:
        ext = path.suffix.lower()
        if ext in _PY:
            py_extractor.extract_file(path)
        elif ext in _JS:
            js_extractor.extract_file(path)

    # 3. Extract UI and cross-layer bindings
    for path in all_files:
        ext = path.suffix.lower()
        if ext in _UI or ext in {".jsx", ".tsx"}:
            ui_extractor.extract_file(path)

    # 4. Extract workflows and repo gates
    wf_extractor.extract_repo_rails()
    wf_extractor.extract_synthetic_workflows()

    # 5. Optionally run architecture checks and ingest violations
    if include_violations and manifest:
        from ..engine import RuleEngine

        engine = RuleEngine(manifest)
        violations = engine.scan(root, strict=True)
        v_extractor = ViolationExtractor(graph)
        v_extractor.ingest_violations(violations)

    return graph
