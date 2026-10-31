"""Static analysis: build a module dependency graph and symbol index.

Pure-Python, no build step. Parses Python with :mod:`ast` and scans the JS/TS
syntax needed for imports, top-level symbols, and primitive references.
"""

from __future__ import annotations

import ast
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

_SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target"}
)
_PY = (".py",)
_JS = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")


@dataclass(frozen=True)
class JSToken:
    kind: str
    value: str | None
    line: int
    start: int
    template_expression: bool = False


_JS_PREFIX_KEYWORDS = frozenset(
    {
        "await", "case", "delete", "do", "else", "in", "instanceof", "new", "of",
        "return", "throw", "typeof", "void", "yield",
    }
)
_JS_REGEX_PREFIXES = frozenset(
    {
        "(", "[", "{", ",", ";", ":", "=", "=>", "!", "?", "&&", "||", "??", "?.",
        "+", "-", "*", "%", "&", "|", "^", "<", ">",
    }
)
_JS_MULTI_PUNCTUATORS = (
    "===", "!==", "&&=", "||=", "??=", "...", "=>", "?.", "&&", "||", "??",
    "==", "!=", "<=", ">=", "++", "--", "+=", "-=", "*=", "/=", "%=", "**",
)
_JS_CONTROL_PARENS = frozenset({"catch", "for", "if", "switch", "while", "with"})
_JS_STATEMENT_STARTS = frozenset({"class", "const", "export", "function", "import", "let", "var"})


def _js_line_starts(src: str) -> list[int]:
    return [0] + [index + 1 for index, char in enumerate(src) if char == "\n"]


def _js_line_at(offset: int, starts: list[int]) -> int:
    return bisect_right(starts, offset)


def _js_identifier_start(char: str) -> bool:
    return char == "_" or char == "$" or char.isalpha() or ord(char) > 127


def _js_identifier_part(char: str) -> bool:
    return _js_identifier_start(char) or char.isdigit()


def _read_js_unicode_escape(src: str, start: int) -> tuple[str, int] | None:
    if not src.startswith("\\u", start):
        return None
    if start + 2 < len(src) and src[start + 2] == "{":
        close = src.find("}", start + 3)
        if close < 0:
            return None
        digits = src[start + 3 : close]
        end = close + 1
    else:
        digits = src[start + 2 : start + 6]
        end = start + 6
        if len(digits) != 4:
            return None
    try:
        codepoint = int(digits, 16)
        if not digits or codepoint > 0x10FFFF:
            return None
        return chr(codepoint), end
    except ValueError:
        return None


def _js_identifier_start_at(src: str, index: int) -> bool:
    escaped = _read_js_unicode_escape(src, index)
    return escaped is not None and _js_identifier_start(escaped[0])


def _consume_js_identifier(src: str, start: int) -> tuple[str, int]:
    output: list[str] = []
    index = start
    while index < len(src):
        escaped = _read_js_unicode_escape(src, index) if src[index] == "\\" else None
        char, end = escaped if escaped is not None else (src[index], index + 1)
        valid = _js_identifier_start(char) if not output else _js_identifier_part(char)
        if not valid:
            break
        output.append(char)
        index = end
    return "".join(output), index


def _consume_quoted(src: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(src):
        if src[index] == "\\":
            index += 2
        elif src[index] == quote:
            return index + 1
        else:
            index += 1
    return len(src)


def _consume_regex(src: str, start: int) -> int | None:
    index = start + 1
    in_class = False
    while index < len(src) and src[index] not in "\r\n":
        char = src[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(src) and _js_identifier_part(src[index]):
                index += 1
            return index
        index += 1
    return None


def _regex_can_start(previous: JSToken | None, tokens: list[JSToken]) -> bool:
    if previous is None:
        return True
    if previous.kind == "IDENT" and previous.value in _JS_PREFIX_KEYWORDS:
        return True
    if previous.value == ")":
        depth = 0
        for index in range(len(tokens) - 1, -1, -1):
            if tokens[index].value == ")":
                depth += 1
            elif tokens[index].value == "(":
                depth -= 1
                if depth == 0:
                    return index > 0 and tokens[index - 1].value in _JS_CONTROL_PARENS
    return previous.value in _JS_REGEX_PREFIXES


def _decode_js_string(raw: str) -> str:
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    output: list[str] = []
    index = 0
    while index < len(raw):
        if raw[index] != "\\" or index + 1 >= len(raw):
            output.append(raw[index])
            index += 1
            continue
        char = raw[index + 1]
        if char in "\r\n":
            index += 2
            if char == "\r" and index < len(raw) and raw[index] == "\n":
                index += 1
            continue
        if char == "x" and index + 3 < len(raw):
            try:
                output.append(chr(int(raw[index + 2 : index + 4], 16)))
                index += 4
                continue
            except ValueError:
                pass
        if char == "u":
            if index + 2 < len(raw) and raw[index + 2] == "{":
                close = raw.find("}", index + 3)
                if close > 0:
                    try:
                        output.append(chr(int(raw[index + 3 : close], 16)))
                        index = close + 1
                        continue
                    except (ValueError, OverflowError):
                        pass
            elif index + 5 < len(raw):
                try:
                    output.append(chr(int(raw[index + 2 : index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        output.append(escapes.get(char, char))
        index += 2
    return "".join(output)


def _scan_js_template(
    src: str, start: int, line_starts: list[int], template_expression: bool
) -> tuple[list[JSToken], int, str | None]:
    tokens: list[JSToken] = []
    literal: list[str] = []
    has_expression = False
    index = start + 1
    while index < len(src):
        char = src[index]
        if char == "\\":
            if index + 1 < len(src):
                literal.extend((char, src[index + 1]))
            index += 2
        elif char == "`":
            value = _decode_js_string("".join(literal))
            if has_expression:
                tokens.append(
                    JSToken(
                        "TEMPLATE_END",
                        None,
                        _js_line_at(index, line_starts),
                        index,
                        template_expression,
                    )
                )
            return tokens, index + 1, None if has_expression else value
        elif src.startswith("${", index):
            has_expression = True
            expression, index = _scan_js_region(src, index + 2, line_starts, True, True)
            tokens.extend(expression)
        else:
            literal.append(char)
            index += 1
    value = _decode_js_string("".join(literal))
    return tokens, len(src), None if has_expression else value


def _scan_js_region(
    src: str,
    start: int,
    line_starts: list[int],
    template_expression: bool = False,
    stop_at_brace: bool = False,
) -> tuple[list[JSToken], int]:
    tokens: list[JSToken] = []
    index = start
    brace_depth = 0
    previous: JSToken | None = None
    while index < len(src):
        char = src[index]
        if char.isspace():
            index += 1
            continue
        if index == 0 and src.startswith("#!", index):
            newline = src.find("\n", index + 2)
            index = len(src) if newline < 0 else newline + 1
            continue
        if stop_at_brace and char == "}" and brace_depth == 0:
            return tokens, index + 1
        if src.startswith("//", index):
            newline = src.find("\n", index + 2)
            index = len(src) if newline < 0 else newline + 1
            continue
        if src.startswith("/*", index):
            close = src.find("*/", index + 2)
            index = len(src) if close < 0 else close + 2
            continue
        nested, token, index = _scan_js_token(
            src, index, line_starts, template_expression, previous, tokens
        )
        tokens.append(token)
        tokens.extend(nested)
        previous = nested[-1] if nested else token
        if token.value == "{":
            brace_depth += 1
        elif token.value == "}" and brace_depth:
            brace_depth -= 1
    return tokens, index


def _scan_js_token(
    src: str,
    start: int,
    line_starts: list[int],
    template_expression: bool,
    previous: JSToken | None,
    tokens: list[JSToken],
) -> tuple[list[JSToken], JSToken, int]:
    char = src[start]
    index = start
    nested: list[JSToken] = []
    if char in "\"'":
        index = _consume_quoted(src, start, char)
        value = _decode_js_string(src[start + 1 : index - 1])
        token = JSToken("STRING", value, _js_line_at(start, line_starts), start, template_expression)
    elif char == "`":
        nested, index, value = _scan_js_template(src, start, line_starts, template_expression)
        token = JSToken("TEMPLATE", value, _js_line_at(start, line_starts), start, template_expression)
    elif char == "/" and _regex_can_start(previous, tokens):
        regex_end = _consume_regex(src, start)
        if regex_end is None:
            index = start + 1
            token = JSToken("PUNCT", char, _js_line_at(start, line_starts), start, template_expression)
        else:
            index = regex_end
            token = JSToken("REGEX", None, _js_line_at(start, line_starts), start, template_expression)
    elif _js_identifier_start(char) or _js_identifier_start_at(src, start):
        value, index = _consume_js_identifier(src, start)
        token = JSToken("IDENT", value, _js_line_at(start, line_starts), start, template_expression)
    elif char.isdigit():
        index = start + 1
        while index < len(src) and (src[index].isalnum() or src[index] in "._"):
            index += 1
        token = JSToken("NUMBER", src[start:index], _js_line_at(start, line_starts), start, template_expression)
    else:
        value = next(
            (operator for operator in _JS_MULTI_PUNCTUATORS if src.startswith(operator, start)),
            char,
        )
        index = start + len(value)
        token = JSToken("PUNCT", value, _js_line_at(start, line_starts), start, template_expression)
    return nested, token, index


def lex_js(src: str) -> list[JSToken]:
    """Tokenize the JS/TS syntax needed by the dependency and symbol scanners."""
    line_starts = _js_line_starts(src)
    tokens, _ = _scan_js_region(src, 0, line_starts)
    return tokens


def _brace_depths(tokens: list[JSToken]) -> list[int]:
    depths: list[int] = []
    depth = 0
    for token in tokens:
        depths.append(depth)
        if token.value == "{":
            depth += 1
        elif token.value == "}":
            depth = max(0, depth - 1)
    return depths


def _static_import_spec(
    tokens: list[JSToken], depths: list[int], start: int
) -> str | None:
    base_depth = depths[start]
    if start + 1 < len(tokens) and tokens[start + 1].kind == "STRING":
        return tokens[start + 1].value
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token.value == ";" and depths[index] == base_depth:
            return None
        if index > start + 1 and depths[index] == base_depth:
            if token.value in _JS_STATEMENT_STARTS and token.line > tokens[index - 1].line:
                return None
        if token.kind == "IDENT" and token.value == "from" and depths[index] == base_depth:
            if index + 1 < len(tokens) and tokens[index + 1].kind == "STRING":
                return tokens[index + 1].value
            return None
        if token.kind == "STRING" and depths[index] == base_depth:
            return None
    return None


def _extract_js_imports(tokens: list[JSToken]) -> list[tuple[str, int, bool]]:
    imports: list[tuple[str, int, bool]] = []
    depths = _brace_depths(tokens)
    for index, token in enumerate(tokens):
        if token.kind != "IDENT":
            continue
        previous = tokens[index - 1] if index else None
        if previous and previous.value in {".", "?."}:
            continue
        if token.value in {"import", "require"} and index + 2 < len(tokens):
            if tokens[index + 1].value == "(":
                spec = tokens[index + 2]
                if spec.kind in {"STRING", "TEMPLATE"} and spec.value is not None:
                    imports.append((spec.value, token.line, True))
                continue
        if token.template_expression:
            continue
        if token.value == "import":
            if index + 1 < len(tokens) and tokens[index + 1].kind == "STRING":
                if tokens[index + 1].value is not None:
                    imports.append((tokens[index + 1].value, token.line, False))
            elif depths[index] == 0:
                spec = _static_import_spec(tokens, depths, index)
                if spec:
                    imports.append((spec, token.line, False))
        elif token.value == "export" and depths[index] == 0:
            if index + 1 < len(tokens) and tokens[index + 1].value == "default":
                continue
            spec = _static_import_spec(tokens, depths, index)
            if spec:
                imports.append((spec, token.line, False))
    return imports


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


def _find_assignment(tokens: list[JSToken], start: int) -> int | None:
    parens = brackets = braces = angles = 0
    for index in range(start, len(tokens)):
        value = tokens[index].value
        if index > start and tokens[index].line > tokens[index - 1].line:
            if value in _JS_STATEMENT_STARTS:
                return None
        if value == "=" and not (parens or brackets or braces or angles):
            return index
        if value in {";", ","} and not (parens or brackets or braces or angles):
            return None
        parens += (value == "(") - (value == ")")
        brackets += (value == "[") - (value == "]")
        braces += (value == "{") - (value == "}")
        angles += (value == "<") - (value == ">")
    return None


def _find_declarator_end(tokens: list[JSToken], start: int) -> int:
    parens = brackets = braces = angles = 0
    for index in range(start, len(tokens)):
        value = tokens[index].value
        if index > start and tokens[index].line > tokens[index - 1].line:
            if value in _JS_STATEMENT_STARTS:
                return index
        if value in {",", ";"} and not (parens or brackets or braces or angles):
            return index
        parens += (value == "(") - (value == ")")
        brackets += (value == "[") - (value == "]")
        braces += (value == "{") - (value == "}")
        angles += (value == "<") - (value == ">")
    return len(tokens)


def _is_arrow_initializer(tokens: list[JSToken], start: int) -> bool:
    index = start
    if index < len(tokens) and tokens[index].value == "async":
        index += 1
    if index < len(tokens) and tokens[index].value == "<":
        generic_end = _matching_token(tokens, index, "<", ">")
        if generic_end is None:
            return False
        index = generic_end + 1
    if index < len(tokens) and tokens[index].value == "(":
        close = _matching_token(tokens, index, "(", ")")
        if close is None:
            return False
        index = close + 1
    elif index < len(tokens) and tokens[index].kind == "IDENT":
        index += 1
    else:
        return False
    if index < len(tokens) and tokens[index].value == ":":
        index += 1
        nesting = 0
        while index < len(tokens):
            value = tokens[index].value
            if value == "=>" and nesting == 0:
                return True
            if value in {"(", "[", "{", "<"}:
                nesting += 1
            elif value in {")", "]", "}", ">"}:
                nesting = max(0, nesting - 1)
            elif value in {";", ","} and nesting == 0:
                return False
            index += 1
        return False
    return index < len(tokens) and tokens[index].value == "=>"


def _top_level_js_symbols(
    tokens: list[JSToken],
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    functions: list[tuple[str, int]] = []
    classes: list[tuple[str, int]] = []
    depths = _brace_depths(tokens)
    expression_predecessors = {
        "=", "(", "[", ",", ":", "?", "??", "await", "new", "return", "throw", "yield", "=>",
    }
    parens = brackets = 0
    for index, token in enumerate(tokens):
        if (
            token.kind == "IDENT"
            and not token.template_expression
            and depths[index] == 0
            and not parens
            and not brackets
        ):
            previous = tokens[index - 1].value if index else None
            if token.value == "function":
                before_previous = tokens[index - 2].value if index > 1 else None
                if previous not in expression_predecessors and not (
                    previous == "async" and before_previous in expression_predecessors
                ):
                    name_index = index + 1 + (index + 1 < len(tokens) and tokens[index + 1].value == "*")
                    if name_index < len(tokens) and tokens[name_index].kind == "IDENT":
                        functions.append((tokens[name_index].value or "", token.line))
            elif token.value == "class":
                if previous not in expression_predecessors:
                    if index + 1 < len(tokens) and tokens[index + 1].kind == "IDENT":
                        classes.append((tokens[index + 1].value or "", token.line))
            elif token.value in {"const", "let", "var"} and index + 1 < len(tokens):
                declaration = index + 1
                while declaration < len(tokens) and tokens[declaration].kind == "IDENT":
                    name = tokens[declaration]
                    assignment = _find_assignment(tokens, declaration + 1)
                    end = _find_declarator_end(tokens, declaration + 1)
                    if assignment is not None and assignment < end and _is_arrow_initializer(tokens, assignment + 1):
                        functions.append((name.value or "", name.line))
                    if end >= len(tokens) or tokens[end].value != ",":
                        break
                    declaration = end + 1
        parens += (token.value == "(") - (token.value == ")")
        brackets += (token.value == "[") - (token.value == "]")
    return functions, classes


@dataclass
class Module:
    path: str
    imports: set[str] = field(default_factory=set)
    import_locations: list[tuple[str, int]] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    symbol_locations: list[tuple[str, int]] = field(default_factory=list)


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
        module.symbol_locations.append((node.name, node.lineno))
        return
    if isinstance(node, ast.ClassDef) and _top_level(node, tree):
        module.classes.append(node.name)
        module.symbol_locations.append((node.name, node.lineno))
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
    tokens = lex_js(text)
    for spec, line, _dynamic in _extract_js_imports(tokens):
        module.imports.add(spec)
        module.import_locations.append((spec, line))
    functions, classes = _top_level_js_symbols(tokens)
    module.functions.extend(name for name, _ in functions)
    module.classes.extend(name for name, _ in classes)
    module.symbol_locations.extend(functions)
    module.symbol_locations.extend(classes)
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
