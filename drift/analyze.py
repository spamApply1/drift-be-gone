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

_JS_TOKEN_SPEC = [
    ("COMMENT", r"/\*[\s\S]*?\*/|//[^\r\n]*"),
    ("STRING", r'"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`'),
    ("STATIC_IMPORT", r"""(?:import|export)\s+(?:(?:(?!\bfrom\b)[^;])*?\bfrom\s+)?[\x27"]([^\x27"]+)[\x27"]"""),
    ("CALL_IMPORT", r"""\b(?:require|import)\s*\(\s*[\x27"]([^\x27"]+)[\x27"]\s*\)"""),
]
_JS_TOKEN_REGEX = re.compile("|".join(f"(?P<{pair[0]}>{pair[1]})" for pair in _JS_TOKEN_SPEC))

_JS_FN = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_JS_CLASS = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_JS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?\s*=\s*"
    r"(?:async\s*)?(?:<[^>]*>)?\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?\s*=>",
    re.MULTILINE,
)


def _strip_js_comments(src: str) -> str:
    pattern = r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)|(/\*[\s\S]*?\*/|//[^\r\n]*)'

    def replacer(m: re.Match) -> str:
        if m.group(2):
            s = m.group(2)
            return "\n" * s.count("\n") + " " * (len(s) - s.count("\n"))
        return m.group(1)

    return re.sub(pattern, replacer, src)


def _clean_js_for_primitives(src: str) -> str:
    pattern = (
        r'(/\*[\s\S]*?\*/|//[^\r\n]*)'
        r'|(\[\s*(?:"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27)\s*\])'
        r'|("(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`)'
    )

    def replacer(m: re.Match) -> str:
        if m.group(1):
            s = m.group(1)
            return "\n" * s.count("\n") + " " * (len(s) - s.count("\n"))
        if m.group(2):
            return m.group(2)
        if m.group(3):
            s = m.group(3)
            return "\n" * s.count("\n") + " " * (len(s) - s.count("\n"))
        return m.group(0)

    return re.sub(pattern, replacer, src)


def _extract_js_imports(src: str) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    for match in _JS_TOKEN_REGEX.finditer(src):
        kind = match.lastgroup
        if kind == "STATIC_IMPORT":
            spec = match.group("STATIC_IMPORT")
            target = re.search(r"""[\x27"]([^\x27"]+)[\x27"]\s*$""", spec)
            if target:
                line = src[:match.start()].count("\n") + 1
                results.append((target.group(1), line))
        elif kind == "CALL_IMPORT":
            spec = match.group("CALL_IMPORT")
            target = re.search(r"""[\x27"]([^\x27"]+)[\x27"]""", spec)
            if target:
                line = src[:match.start()].count("\n") + 1
                results.append((target.group(1), line))
    return results


@dataclass
class Module:
    path: str
    imports: set[str] = field(default_factory=set)
    import_locations: list[tuple[str, int]] = field(default_factory=list)
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
        return
    if isinstance(node, ast.ClassDef) and _top_level(node, tree):
        module.classes.append(node.name)
        return
    if isinstance(node, ast.Import):
        for alias in node.names:
            module.imports.add(alias.name)
            module.import_locations.append((alias.name, node.lineno))
        return
    if isinstance(node, ast.ImportFrom) and node.module:
        module.imports.add(node.module)
        module.import_locations.append((node.module, node.lineno))
        module.imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        return
    if isinstance(node, ast.Call):
        target = _call_import_target(node)
        if target:
            module.imports.add(target)
            module.import_locations.append((target, node.lineno))


def _call_import_target(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "__import__" and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    elif isinstance(func, ast.Attribute) and func.attr == "import_module" and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _top_level(node: ast.AST, tree: ast.AST) -> bool:
    return node in getattr(tree, "body", [])


def _analyze_js(path: Path, rel: str) -> Module:
    module = Module(path=rel)
    text = path.read_text(encoding="utf-8", errors="ignore")
    for spec, line in _extract_js_imports(text):
        module.imports.add(spec)
        module.import_locations.append((spec, line))
    cleaned = _clean_js_for_primitives(text)
    module.functions.extend(_JS_FN.findall(cleaned))
    module.functions.extend(_JS_ARROW.findall(cleaned))
    module.classes.extend(_JS_CLASS.findall(cleaned))
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
