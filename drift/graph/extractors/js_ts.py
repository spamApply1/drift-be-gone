"""JavaScript and TypeScript extractor for the knowledge graph.

Extracts module nodes, class/function declarations, exports, and import dependencies
from .js, .mjs, .cjs, .ts, .tsx, .jsx files.
"""

from __future__ import annotations

import os
import re
from bisect import bisect_left
from pathlib import Path
from typing import Any

from ...analyze import JSToken, _extract_js_imports, _top_level_js_symbols, lex_js
from ..model import Edge, Node, PropertyGraph

_FN_DECL = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_CLASS_DECL = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$]*))?",
    re.MULTILINE,
)
_ARROW_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?\s*=\s*"
    r"(?:async\s*)?(?:<[^>]*>)?\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?\s*=>",
    re.MULTILINE,
)
_CONTROL_WORDS = frozenset(
    {
        "await", "break", "case", "catch", "const", "continue", "do", "else",
        "finally", "for", "if", "let", "new", "return", "switch", "throw",
        "try", "var", "while", "yield",
    }
)
_SHAPE_OPERATORS = frozenset(
    {
        "!", "!=", "!==", "%", "&&", "*", "+", "+=", "-", "-=", "/", "<", "<=",
        "=", "==", "===", ">", ">=", "?", "??", "||",
    }
)
_JS_BUILTINS = frozenset(
    {
        "Array", "Boolean", "Date", "Error", "JSON", "Math", "Number", "Object",
        "Promise", "RegExp", "String", "fetch", "parseFloat", "parseInt", "setTimeout",
    }
)


def _matching_token(tokens: list[JSToken], start: int, left: str, right: str) -> int | None:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].value == left:
            depth += 1
        elif tokens[index].value == right:
            depth -= 1
            if depth == 0:
                return index
    return None


def _expression_end(tokens: list[JSToken], start: int) -> int:
    parens = brackets = braces = 0
    statement_starts = {"class", "const", "export", "function", "import", "let", "var"}
    for index in range(start, len(tokens)):
        token = tokens[index]
        if (
            index > start
            and token.line > tokens[index - 1].line
            and token.value in statement_starts
        ):
            return index
        if token.value in {",", ";"} and not (parens or brackets or braces):
            return index
        parens += (token.value == "(") - (token.value == ")")
        brackets += (token.value == "[") - (token.value == "]")
        braces += (token.value == "{") - (token.value == "}")
    return len(tokens)


def _function_body(tokens: list[JSToken], start: int, arrow: bool) -> tuple[list[JSToken], int | None]:
    if arrow:
        marker = next((i for i in range(start, len(tokens)) if tokens[i].value == "=>"), None)
        if marker is None:
            return [], None
        body_start = marker + 1
    else:
        open_params = next((i for i in range(start, len(tokens)) if tokens[i].value == "("), None)
        if open_params is None:
            return [], None
        close_params = _matching_token(tokens, open_params, "(", ")")
        if close_params is None:
            return [], None
        body_start = next(
            (i for i in range(close_params + 1, len(tokens)) if tokens[i].value == "{"),
            len(tokens),
        )

    if body_start < len(tokens) and tokens[body_start].value == "{":
        close_body = _matching_token(tokens, body_start, "{", "}")
        if close_body is None:
            return tokens[body_start + 1 :], tokens[-1].line if tokens else None
        return tokens[body_start + 1 : close_body], tokens[close_body].line

    end = _expression_end(tokens, body_start)
    body = tokens[body_start:end]
    return body, body[-1].line if body else None


def _intent_signature(tokens: list[JSToken]) -> tuple[list[str], list[str]]:
    shape: list[str] = []
    calls: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind == "STRING":
            shape.append("literal:string")
        elif token.kind == "NUMBER":
            shape.append("literal:number")
        elif token.kind == "IDENT":
            if token.value in _CONTROL_WORDS:
                shape.append(f"control:{token.value}")
            elif index + 1 < len(tokens) and tokens[index + 1].value == "(":
                shape.append("call")
                if index and tokens[index - 1].value in {".", "?."}:
                    calls.add(f"method:{token.value}")
                elif token.value in _JS_BUILTINS:
                    calls.add(f"builtin:{token.value}")
                else:
                    calls.add("function-call")
            else:
                shape.append("identifier")
        elif token.value in _SHAPE_OPERATORS:
            shape.append(f"op:{token.value}")
        elif token.value in {"(", ")", "[", "]", "{", "}"}:
            shape.append("group")
        elif token.value in {".", "?."}:
            shape.append("member-access")
    return shape[:256], sorted(calls)


class JSTSExtractor:
    """Extracts nodes and edges from JavaScript/TypeScript source files."""

    def __init__(self, root: Path, graph: PropertyGraph) -> None:
        self.root = root
        self.graph = graph
        self.known_files: set[str] = set()

    def register_module(self, rel_path: str) -> None:
        self.known_files.add(rel_path)

    def _resolve_js_target(self, source_rel: str, spec: str) -> str | None:
        if not spec.startswith("."):
            return None
        base = Path(source_rel).parent
        resolved = os.path.normpath((base / spec).as_posix())
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.js"):
            candidate = f"{resolved}{suffix}"
            if candidate in self.known_files:
                return candidate
        return None

    def extract_file(self, path: Path) -> None:
        rel = path.relative_to(self.root).as_posix()
        mod_id = f"module:{rel}"
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        tokens = lex_js(content)
        token_starts = [token.start for token in tokens]

        lines = content.splitlines()
        loc = len(lines)

        self.graph.add_node(
            Node(
                id=mod_id,
                kind="module",
                name=rel,
                path=rel,
                line=1,
                end_line=loc,
                properties={"language": "javascript/typescript", "loc": loc},
            )
        )

        functions, classes = _top_level_js_symbols(tokens)
        valid_functions = set(functions)
        valid_classes = set(classes)

        # Extract Imports
        for spec, line, is_dynamic in _extract_js_imports(tokens):
            target_rel = self._resolve_js_target(rel, spec)
            if target_rel:
                self.graph.add_edge(
                    Edge(
                        source=mod_id,
                        target=f"module:{target_rel}",
                        kind="IMPORTS",
                        line=line,
                        properties={"spec": spec, "dynamic": is_dynamic},
                    )
                )

        # Extract Classes
        matched_classes: set[tuple[str, int]] = set()
        for match in _CLASS_DECL.finditer(content):
            cls_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            if (cls_name, line) not in valid_classes:
                continue
            matched_classes.add((cls_name, line))
            extends_name = match.group(2)
            cls_id = f"class:{rel}:{cls_name}"
            self.graph.add_node(
                Node(
                    id=cls_id,
                    kind="class",
                    name=cls_name,
                    path=rel,
                    line=line,
                    properties={"extends": extends_name},
                )
            )
            self.graph.add_edge(Edge(source=mod_id, target=cls_id, kind="DEFINES", line=line))

        for cls_name, line in classes:
            if (cls_name, line) in matched_classes:
                continue
            cls_id = f"class:{rel}:{cls_name}"
            self.graph.add_node(
                Node(
                    id=cls_id,
                    kind="class",
                    name=cls_name,
                    path=rel,
                    line=line,
                    properties={"language": "javascript/typescript", "extends": None},
                )
            )
            self.graph.add_edge(Edge(source=mod_id, target=cls_id, kind="DEFINES", line=line))

        # Extract Functions
        matched_functions: set[tuple[str, int]] = set()
        for match in _FN_DECL.finditer(content):
            fn_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            if (fn_name, line) not in valid_functions:
                continue
            matched_functions.add((fn_name, line))
            start = bisect_left(token_starts, match.start())
            body, end_line = _function_body(tokens, start, arrow=False)
            intent_shape, intent_calls = _intent_signature(body)
            fn_id = f"fn:{rel}:{fn_name}"
            self.graph.add_node(
                Node(
                    id=fn_id,
                    kind="function",
                    name=fn_name,
                    path=rel,
                    line=line,
                    end_line=end_line,
                    properties={
                        "language": "javascript/typescript",
                        "intent_shape": intent_shape,
                        "intent_calls": intent_calls,
                    },
                )
            )
            self.graph.add_edge(Edge(source=mod_id, target=fn_id, kind="DEFINES", line=line))

        for match in _ARROW_DECL.finditer(content):
            fn_name = match.group(1)
            line = content[: match.start()].count("\n") + 1
            if (fn_name, line) not in valid_functions:
                continue
            matched_functions.add((fn_name, line))
            start = bisect_left(token_starts, match.start())
            body, end_line = _function_body(tokens, start, arrow=True)
            intent_shape, intent_calls = _intent_signature(body)
            fn_id = f"fn:{rel}:{fn_name}"
            self.graph.add_node(
                Node(
                    id=fn_id,
                    kind="function",
                    name=fn_name,
                    path=rel,
                    line=line,
                    end_line=end_line,
                    properties={
                        "language": "javascript/typescript",
                        "intent_shape": intent_shape,
                        "intent_calls": intent_calls,
                    },
                )
            )
            self.graph.add_edge(Edge(source=mod_id, target=fn_id, kind="DEFINES", line=line))

        for fn_name, line in functions:
            if (fn_name, line) in matched_functions:
                continue
            fn_id = f"fn:{rel}:{fn_name}"
            self.graph.add_node(
                Node(
                    id=fn_id,
                    kind="function",
                    name=fn_name,
                    path=rel,
                    line=line,
                    properties={
                        "language": "javascript/typescript",
                        "intent_shape": [],
                        "intent_calls": [],
                    },
                )
            )
            self.graph.add_edge(Edge(source=mod_id, target=fn_id, kind="DEFINES", line=line))
