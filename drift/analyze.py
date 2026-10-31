"""Static analysis: build a module dependency graph and symbol index.

Pure-Python, no build step. Parses Python imports via the ast module and
JavaScript/TypeScript imports via lightweight regex, producing the graph and
symbol data that both the architecture rules and the understanding map consume.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target"}
)
_PY = (".py",)
_JS = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
_JS_IMPORT = re.compile(r"""(?:import[^'"]*from|import|require\()\s*['"]([^'"]+)['"]""")


@dataclass
class Module:
    path: str
    imports: set[str] = field(default_factory=set)
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)


@dataclass
class Graph:
    modules: dict[str, Module]
    edges: list[tuple[str, str]]

    def domains(self) -> list[str]:
        tops = {path.split("/", 1)[0] for path in self.modules if "/" in path}
        return sorted(tops)

    def in_degree(self) -> dict[str, int]:
        counts = {path: 0 for path in self.modules}
        for _, dst in self.edges:
            counts[dst] = counts.get(dst, 0) + 1
        return counts


def iter_source(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _PY + _JS:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return sorted(files)


def _python_module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_python(target: str, rel: Path, index: dict[str, str]) -> str | None:
    if target in index:
        return index[target]
    parent = _python_module_name(rel).rsplit(".", 1)[0] if "." in _python_module_name(rel) else ""
    combined = f"{parent}.{target}" if parent else target
    return index.get(combined)


def _analyze_python(path: Path, rel: str, root: Path) -> Module:
    module = Module(path=rel)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return module
    for node in ast.walk(tree):
        _classify_python_node(node, tree, module)
    return module


def _classify_python_node(node: ast.AST, tree: ast.AST, module: Module) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _top_level(node, tree):
        module.functions.append(node.name)
    elif isinstance(node, ast.ClassDef) and _top_level(node, tree):
        module.classes.append(node.name)
    elif isinstance(node, ast.Import):
        module.imports.update(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module:
        module.imports.add(node.module)
        module.imports.update(f"{node.module}.{alias.name}" for alias in node.names)


def _top_level(node: ast.AST, tree: ast.AST) -> bool:
    return node in getattr(tree, "body", [])


def _analyze_js(path: Path, rel: str) -> Module:
    module = Module(path=rel)
    text = path.read_text(encoding="utf-8", errors="ignore")
    module.imports.update(_JS_IMPORT.findall(text))
    module.functions.extend(re.findall(r"function\s+([A-Za-z_$][\w$]*)", text))
    _arrow_fn = (
        r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    )
    module.functions.extend(re.findall(_arrow_fn, text))
    module.classes.extend(re.findall(r"class\s+([A-Za-z_$][\w$]*)", text))
    return module


def _js_target(source_rel: str, spec: str, known: set[str]) -> str | None:
    if not spec.startswith("."):
        return None
    base = Path(source_rel).parent
    resolved = (base / spec).as_posix()
    for suffix in ("", ".js", ".mjs", ".ts", ".tsx", ".jsx", "/index.js", "/index.ts"):
        candidate = f"{resolved}{suffix}"
        if candidate in known:
            return candidate
    return None


def build_graph(root: Path) -> Graph:
    """Analyze *root* and return the resolved module dependency graph."""

    files = iter_source(root)
    modules: dict[str, Module] = {}
    py_index: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        if path.suffix in _PY:
            modules[rel] = _analyze_python(path, rel, root)
            py_index[_python_module_name(path.relative_to(root))] = rel
        else:
            modules[rel] = _analyze_js(path, rel)

    known = set(modules)
    edges: list[tuple[str, str]] = []
    for rel, module in modules.items():
        for spec in sorted(module.imports):
            if rel.endswith(_PY):
                target = _resolve_python(spec, Path(rel), py_index)
            else:
                target = _js_target(rel, spec, known)
            if target and target != rel:
                edges.append((rel, target))
    return Graph(modules=modules, edges=sorted(set(edges)))
