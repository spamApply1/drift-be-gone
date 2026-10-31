"""Python AST extractor for the knowledge graph.

Parses Python source files using standard library `ast`, extracting:
- Module nodes
- Class and Function definitions (with params, line bounds, complexity)
- INHERITS edges between classes
- IMPORTS edges with symbol details
- CALLS edges between functions and classes
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..model import Edge, Node, PropertyGraph


def _python_module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _get_call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return None


def _intent_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], list[str]]:
    """Return a name-insensitive AST shape and call vocabulary for a function."""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]

    shape = [f"args:{len(node.args.posonlyargs) + len(node.args.args) + len(node.args.kwonlyargs)}"]
    if isinstance(node, ast.AsyncFunctionDef):
        shape.append("async")
    calls: set[str] = set()

    def visit(current: ast.AST) -> None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            shape.append("nested-definition")
            return
        if isinstance(current, ast.stmt):
            shape.append(f"stmt:{type(current).__name__}")
        elif isinstance(current, ast.operator):
            shape.append(f"op:{type(current).__name__}")
        elif isinstance(current, ast.unaryop):
            shape.append(f"op:{type(current).__name__}")
        elif isinstance(current, ast.boolop):
            shape.append(f"op:{type(current).__name__}")
        elif isinstance(current, ast.cmpop):
            shape.append(f"op:{type(current).__name__}")
        elif isinstance(current, ast.Constant):
            shape.append(f"literal:{type(current.value).__name__}")
        elif isinstance(current, ast.Attribute):
            shape.append("attribute")
        elif isinstance(current, ast.Name):
            shape.append("name")
        elif isinstance(current, ast.Call):
            shape.append("call")
            name = _get_call_name(current)
            if name:
                if isinstance(current.func, ast.Attribute):
                    calls.add(f"method:{current.func.attr}")
                elif name in {
                    "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
                    "len", "list", "map", "max", "min", "open", "print", "range",
                    "set", "sorted", "str", "sum", "tuple", "zip",
                }:
                    calls.add(f"builtin:{name}")
                else:
                    calls.add("function-call")
        elif isinstance(current, ast.expr):
            shape.append(f"expr:{type(current).__name__}")

        for child in ast.iter_child_nodes(current):
            visit(child)
            if len(shape) >= 256:
                return

    for statement in body:
        visit(statement)
        if len(shape) >= 256:
            break
    return shape, sorted(calls)


class PythonASTExtractor:
    """Extracts fine-grained symbol and dependency nodes/edges from Python files."""

    def __init__(self, root: Path, graph: PropertyGraph) -> None:
        self.root = root
        self.graph = graph
        self.module_index: dict[str, str] = {}  # "pkg.foo" -> "pkg/foo.py"

    def register_module(self, rel_path: str) -> None:
        mod_name = _python_module_name(Path(rel_path))
        self.module_index[mod_name] = rel_path

    def extract_file(self, path: Path) -> None:
        rel = path.relative_to(self.root).as_posix()
        mod_id = f"module:{rel}"
        mod_name = _python_module_name(path.relative_to(self.root))

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content)
        except (OSError, SyntaxError):
            self.graph.add_node(
                Node(
                    id=mod_id,
                    kind="module",
                    name=mod_name or rel,
                    path=rel,
                    line=1,
                    properties={"language": "python", "parse_error": True},
                )
            )
            return

        lines = content.splitlines()
        loc = len(lines)

        self.graph.add_node(
            Node(
                id=mod_id,
                kind="module",
                name=mod_name or rel,
                path=rel,
                line=1,
                end_line=loc,
                properties={"language": "python", "loc": loc},
            )
        )

        # Track local definitions for intra-module call resolution
        local_symbols: dict[str, str] = {}  # name -> node_id
        # Track imported names: alias -> (target_module, original_name)
        imported_symbols: dict[str, tuple[str, str]] = {}

        # First pass: imports, classes, functions
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    target_mod = alias.name
                    as_name = alias.asname or alias.name
                    imported_symbols[as_name] = (target_mod, "")
                    target_rel = self.module_index.get(target_mod)
                    if target_rel:
                        self.graph.add_edge(
                            Edge(
                                source=mod_id,
                                target=f"module:{target_rel}",
                                kind="IMPORTS",
                                line=stmt.lineno,
                                properties={"symbol": alias.name, "alias": alias.asname},
                            )
                        )
            elif isinstance(stmt, ast.ImportFrom):
                module_name = stmt.module or ""
                # Resolve relative imports
                if stmt.level > 0:
                    current_pkg = list(Path(rel).parent.parts)
                    level = stmt.level - 1
                    base_parts = current_pkg[: len(current_pkg) - level] if level <= len(current_pkg) else []
                    if module_name:
                        base_parts.append(module_name)
                    module_name = ".".join(base_parts)

                target_rel = self.module_index.get(module_name)
                for alias in stmt.names:
                    as_name = alias.asname or alias.name
                    imported_symbols[as_name] = (module_name, alias.name)
                    if target_rel:
                        self.graph.add_edge(
                            Edge(
                                source=mod_id,
                                target=f"module:{target_rel}",
                                kind="IMPORTS",
                                line=stmt.lineno,
                                properties={"symbol": alias.name, "alias": alias.asname},
                            )
                        )

            elif isinstance(stmt, ast.ClassDef):
                cls_id = f"class:{rel}:{stmt.name}"
                local_symbols[stmt.name] = cls_id
                self.graph.add_node(
                    Node(
                        id=cls_id,
                        kind="class",
                        name=stmt.name,
                        path=rel,
                        line=stmt.lineno,
                        end_line=getattr(stmt, "end_lineno", stmt.lineno),
                        properties={"bases": [ast.unparse(b) for b in stmt.bases]},
                    )
                )
                self.graph.add_edge(Edge(source=mod_id, target=cls_id, kind="DEFINES", line=stmt.lineno))

                # Inheritance edges
                for base in stmt.bases:
                    base_name = ast.unparse(base)
                    if base_name in local_symbols:
                        self.graph.add_edge(
                            Edge(source=cls_id, target=local_symbols[base_name], kind="INHERITS", line=stmt.lineno)
                        )
                    elif base_name in imported_symbols:
                        tgt_mod, sym = imported_symbols[base_name]
                        tgt_rel = self.module_index.get(tgt_mod)
                        if tgt_rel:
                            target_node = f"class:{tgt_rel}:{sym or base_name}"
                            self.graph.add_edge(
                                Edge(source=cls_id, target=target_node, kind="INHERITS", line=stmt.lineno)
                            )

                # Class methods
                for item in stmt.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_id = f"method:{rel}:{stmt.name}.{item.name}"
                        local_symbols[f"{stmt.name}.{item.name}"] = method_id
                        intent_shape, intent_calls = _intent_signature(item)
                        self.graph.add_node(
                            Node(
                                id=method_id,
                                kind="method",
                                name=f"{stmt.name}.{item.name}",
                                path=rel,
                                line=item.lineno,
                                end_line=getattr(item, "end_lineno", item.lineno),
                                properties={
                                    "language": "python",
                                    "is_async": isinstance(item, ast.AsyncFunctionDef),
                                    "args": [a.arg for a in item.args.args],
                                    "intent_shape": intent_shape,
                                    "intent_calls": intent_calls,
                                },
                            )
                        )
                        self.graph.add_edge(Edge(source=cls_id, target=method_id, kind="DEFINES", line=item.lineno))

            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_id = f"fn:{rel}:{stmt.name}"
                local_symbols[stmt.name] = fn_id
                intent_shape, intent_calls = _intent_signature(stmt)
                self.graph.add_node(
                    Node(
                        id=fn_id,
                        kind="function",
                        name=stmt.name,
                        path=rel,
                        line=stmt.lineno,
                        end_line=getattr(stmt, "end_lineno", stmt.lineno),
                        properties={
                            "language": "python",
                            "is_async": isinstance(stmt, ast.AsyncFunctionDef),
                            "args": [a.arg for a in stmt.args.args],
                            "intent_shape": intent_shape,
                            "intent_calls": intent_calls,
                        },
                    )
                )
                self.graph.add_edge(Edge(source=mod_id, target=fn_id, kind="DEFINES", line=stmt.lineno))

        # Second pass: Call resolution inside functions & methods
        def extract_calls(caller_id: str, body: list[ast.stmt]) -> None:
            for node in ast.walk(ast.Module(body=body, type_ignores=[])):
                if isinstance(node, ast.Call):
                    call_name = _get_call_name(node)
                    if not call_name:
                        continue
                    # Check local functions/classes
                    if call_name in local_symbols:
                        self.graph.add_edge(
                            Edge(
                                source=caller_id,
                                target=local_symbols[call_name],
                                kind="CALLS",
                                line=node.lineno,
                            )
                        )
                    # Check imported symbols (e.g. from utils import helper; helper())
                    elif call_name in imported_symbols:
                        tgt_mod, orig_sym = imported_symbols[call_name]
                        tgt_rel = self.module_index.get(tgt_mod)
                        if tgt_rel:
                            # Could be function or class in target module
                            target_id = f"fn:{tgt_rel}:{orig_sym or call_name}"
                            self.graph.add_edge(
                                Edge(
                                    source=caller_id,
                                    target=target_id,
                                    kind="CALLS",
                                    line=node.lineno,
                                    properties={"target_module": tgt_rel},
                                )
                            )
                    # Check module.symbol calls (e.g. import utils; utils.helper())
                    elif "." in call_name:
                        mod_alias, sym = call_name.split(".", 1)
                        if mod_alias in imported_symbols:
                            tgt_mod, _ = imported_symbols[mod_alias]
                            tgt_rel = self.module_index.get(tgt_mod)
                            if tgt_rel:
                                target_id = f"fn:{tgt_rel}:{sym}"
                                self.graph.add_edge(
                                    Edge(
                                        source=caller_id,
                                        target=target_id,
                                        kind="CALLS",
                                        line=node.lineno,
                                        properties={"target_module": tgt_rel},
                                    )
                                )

        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_id = f"fn:{rel}:{stmt.name}"
                extract_calls(fn_id, stmt.body)
            elif isinstance(stmt, ast.ClassDef):
                for item in stmt.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_id = f"method:{rel}:{stmt.name}.{item.name}"
                        extract_calls(method_id, item.body)
