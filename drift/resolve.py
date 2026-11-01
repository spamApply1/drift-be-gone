"""Resolve-imports analysis: find symbols used but never imported or defined.

This module powers the ``resolve-imports`` architecture check. It answers a
question the dependency-graph layer alone cannot: *does every symbol a JS/TS
module calls actually resolve to an import or a local declaration?*

The high-signal, low-false-positive rule it enables is:

    A module calls ``foo(...)`` where ``foo`` is neither imported nor declared
    in that module, **and** ``foo`` is exported by some other module in the same
    repository.

That pattern is almost always a missing import (an agent used a helper from a
sibling module but forgot the ``import`` line), so flagging it catches a whole
class of "works in review, throws ReferenceError at runtime" bugs that pure
import-graph analysis sails straight past.

Everything here is token-based (reusing :func:`drift.analyze.lex_js`) so it
stays dependency-free and language-faithful without a full JS parser.
"""

from __future__ import annotations

from .analyze import JSToken, lex_js

# JS/TS reserved words and control-flow keywords that lex as IDENT tokens but
# are never free symbol references we could resolve to an import.
_JS_KEYWORDS = frozenset(
    {
        "await", "break", "case", "catch", "class", "const", "continue", "debugger",
        "default", "delete", "do", "else", "enum", "export", "extends", "false",
        "finally", "for", "function", "from", "get", "if", "implements", "import",
        "in", "instanceof", "interface", "let", "new", "null", "of", "package",
        "private", "protected", "public", "return", "set", "static", "super",
        "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while",
        "with", "yield", "as", "async", "declare", "namespace", "type", "readonly",
        "abstract", "is", "keyof", "infer", "satisfies",
    }
)

# Runtime globals and well-known builtins that a module may legitimately call
# without importing. Kept deliberately broad to keep false positives near zero.
_JS_GLOBALS = frozenset(
    {
        # ECMAScript builtins
        "Array", "ArrayBuffer", "BigInt", "Boolean", "DataView", "Date", "Error",
        "EvalError", "Float32Array", "Float64Array", "Function", "Infinity",
        "Int8Array", "Int16Array", "Int32Array", "Intl", "JSON", "Map", "Math",
        "NaN", "Number", "Object", "Promise", "Proxy", "RangeError",
        "ReferenceError", "Reflect", "RegExp", "Set", "String", "Symbol",
        "SyntaxError", "TypeError", "URIError", "Uint8Array", "Uint8ClampedArray",
        "Uint16Array", "Uint32Array", "WeakMap", "WeakRef", "WeakSet",
        "BigInt64Array", "BigUint64Array", "FinalizationRegistry",
        "decodeURI", "decodeURIComponent", "encodeURI", "encodeURIComponent",
        "escape", "unescape", "eval", "globalThis", "isFinite", "isNaN",
        "parseFloat", "parseInt", "structuredClone", "queueMicrotask",
        # Timers / microtasks
        "setTimeout", "clearTimeout", "setInterval", "clearInterval",
        "setImmediate", "requestAnimationFrame", "cancelAnimationFrame",
        "requestIdleCallback", "cancelIdleCallback",
        # DOM / BOM
        "window", "document", "navigator", "location", "history", "screen",
        "console", "alert", "confirm", "prompt", "fetch", "FormData", "Headers",
        "Request", "Response", "URL", "URLSearchParams", "Blob", "File",
        "FileReader", "WebSocket", "Worker", "SharedWorker", "MessageChannel",
        "MessagePort", "BroadcastChannel", "Event", "CustomEvent", "EventTarget",
        "AbortController", "AbortSignal", "Image", "Audio", "Option", "Notification",
        "IntersectionObserver", "MutationObserver", "ResizeObserver",
        "PerformanceObserver", "performance", "localStorage", "sessionStorage",
        "indexedDB", "crypto", "atob", "btoa", "getComputedStyle", "matchMedia",
        "scrollTo", "scrollBy", "open", "close", "postMessage", "addEventListener",
        "removeEventListener", "dispatchEvent", "getSelection", "DOMParser",
        "XMLHttpRequest", "XMLSerializer", "TextEncoder", "TextDecoder",
        "MediaRecorder", "MediaStream", "RTCPeerConnection", "RTCSessionDescription",
        "RTCIceCandidate", "AudioContext", "Path2D", "ImageData", "OffscreenCanvas",
        "HTMLElement", "Node", "NodeList", "DocumentFragment", "Range", "Text",
        "CSS", "FontFace", "customElements", "reportError", "WebAssembly",
        # Node / CommonJS / module
        "require", "module", "exports", "process", "Buffer", "__dirname",
        "__filename", "global", "import", "arguments",
    }
)


def _collect_import_bindings(tokens: list[JSToken], start: int) -> tuple[set[str], int]:
    """Collect local names bound by the ``import`` statement at *start*.

    Returns the bound names and the index just past the statement.
    """
    names: set[str] = set()
    index = start + 1
    n = len(tokens)
    if index < n and tokens[index].kind == "STRING":
        return names, index + 1  # side-effect import: `import '...'`

    pending_ident: str | None = None
    while index < n:
        tok = tokens[index]
        if tok.kind == "IDENT" and tok.value == "from":
            # Skip the module specifier string that follows.
            if index + 1 < n:
                index += 1
            break
        if tok.kind == "STRING":  # bare `import defaultExport` has no `from`; also safety
            break
        if tok.value == "{":
            # Named import clause: `{ a, b as c }`.
            index += 1
            expect_name = True
            local = None
            while index < n and tokens[index].value != "}":
                t = tokens[index]
                if t.kind == "IDENT" and t.value not in {"as"}:
                    if expect_name:
                        local = t.value
                        expect_name = False
                    else:
                        local = t.value  # the `as` alias is the local binding
                elif t.value == ",":
                    if local:
                        names.add(local)
                    local = None
                    expect_name = True
                index += 1
            if local:
                names.add(local)
            index += 1  # past '}'
            continue
        if tok.value == "*":
            # `* as ns`
            if index + 2 < n and tokens[index + 1].value == "as" and tokens[index + 2].kind == "IDENT":
                names.add(tokens[index + 2].value or "")
                index += 3
                continue
            index += 1
            continue
        if tok.kind == "IDENT" and tok.value not in _JS_KEYWORDS:
            # Default import binding: `import defaultName, ...`
            names.add(tok.value or "")
            index += 1
            continue
        if tok.value == ";":
            index += 1
            break
        index += 1
    return names, index


def _matching(tokens: list[JSToken], open_idx: int, left: str, right: str) -> int | None:
    depth = 0
    for i in range(open_idx, len(tokens)):
        v = tokens[i].value
        if v == left:
            depth += 1
        elif v == right:
            depth -= 1
            if depth == 0:
                return i
    return None


def _collect_pattern_names(tokens: list[JSToken], lo: int, hi: int) -> set[str]:
    """Collect binding identifiers in a declaration/parameter pattern range.

    Skips object-literal keys (the ``k`` in ``{ k: v }``) and member accesses,
    keeping the bound name on the value side. Over-collecting on the safe side
    (treating more names as bound) is intentional: it suppresses false
    "missing import" reports, never manufactures them.
    """
    names: set[str] = set()
    i = lo
    while i < hi:
        tok = tokens[i]
        if tok.kind == "IDENT" and tok.value and tok.value not in _JS_KEYWORDS:
            prev = tokens[i - 1].value if i > lo else None
            nxt = tokens[i + 1].value if i + 1 < hi else None
            if prev in {".", "?."}:
                i += 1
                continue
            # `{ key: binding }` — a ':' right after means this ident is a key.
            if nxt == ":":
                i += 1
                continue
            names.add(tok.value)
        i += 1
    return names


def _declaration_region_end(tokens: list[JSToken], start: int) -> int:
    """Find the end of a const/let/var declaration statement (exclusive)."""
    n = len(tokens)
    parens = brackets = braces = 0
    i = start
    while i < n:
        v = tokens[i].value
        if v == "(":
            parens += 1
        elif v == ")":
            parens -= 1
        elif v == "[":
            brackets += 1
        elif v == "]":
            brackets -= 1
        elif v == "{":
            braces += 1
        elif v == "}":
            braces -= 1
        elif v == ";" and parens <= 0 and brackets <= 0 and braces <= 0:
            return i
        elif (
            i > start
            and parens <= 0
            and brackets <= 0
            and braces <= 0
            and tokens[i].line > tokens[i - 1].line
            and tokens[i].kind == "IDENT"
            and v in {"const", "let", "var", "function", "class", "return", "import", "export"}
        ):
            return i
        i += 1
    return n


def collect_bindings(tokens: list[JSToken]) -> set[str]:
    """Return every local name bound in a module: imports, declarations, params."""
    names: set[str] = set()
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        if tok.kind != "IDENT":
            i += 1
            continue
        prev = tokens[i - 1].value if i else None
        if prev in {".", "?."}:
            i += 1
            continue
        val = tok.value
        if val in {"import"} and not tok.template_expression:
            bound, i = _collect_import_bindings(tokens, i)
            names |= bound
            continue
        if val in {"function"}:
            j = i + 1
            if j < n and tokens[j].value == "*":
                j += 1
            if j < n and tokens[j].kind == "IDENT" and tokens[j].value not in _JS_KEYWORDS:
                names.add(tokens[j].value or "")
            # parameters
            paren = j + 1 if (j < n and tokens[j].kind == "IDENT") else j
            if paren < n and tokens[paren].value == "(":
                close = _matching(tokens, paren, "(", ")")
                if close:
                    names |= _collect_pattern_names(tokens, paren + 1, close)
            i += 1
            continue
        if val == "class":
            if i + 1 < n and tokens[i + 1].kind == "IDENT" and tokens[i + 1].value not in _JS_KEYWORDS:
                names.add(tokens[i + 1].value or "")
            i += 1
            continue
        if val in {"const", "let", "var"}:
            end = _declaration_region_end(tokens, i + 1)
            # Collect only the binding side of each declarator (before top-level '=').
            names |= _collect_declarator_targets(tokens, i + 1, end)
            # Do NOT skip the initializer: object/function literals assigned to a
            # const (e.g. `export const api = { ... }`) contain their own nested
            # declarations whose locals must also be recorded as bound.
            i += 1
            continue
        if val == "catch":
            if i + 1 < n and tokens[i + 1].value == "(":
                close = _matching(tokens, i + 1, "(", ")")
                if close:
                    names |= _collect_pattern_names(tokens, i + 2, close)
            i += 1
            continue
        # Arrow params: `(a, b) =>` or `a =>`
        if tok.value == "(":
            close = _matching(tokens, i, "(", ")")
            if close is not None and close + 1 < n and tokens[close + 1].value == "=>":
                names |= _collect_pattern_names(tokens, i + 1, close)
        i += 1
    # Single-identifier arrow params: `x => ...`
    for idx in range(1, n):
        if tokens[idx].value == "=>":
            prev_tok = tokens[idx - 1]
            if prev_tok.kind == "IDENT" and prev_tok.value and prev_tok.value not in _JS_KEYWORDS:
                before = tokens[idx - 2].value if idx >= 2 else None
                if before != ")":
                    names.add(prev_tok.value)
    return names


def _collect_declarator_targets(tokens: list[JSToken], lo: int, hi: int) -> set[str]:
    """Collect binding names from comma-separated declarators in [lo, hi)."""
    names: set[str] = set()
    i = lo
    seg_start = lo
    parens = brackets = braces = 0
    assigned = False
    while i <= hi:
        at_end = i == hi
        v = tokens[i].value if not at_end else None
        if not at_end:
            if v == "(":
                parens += 1
            elif v == ")":
                parens -= 1
            elif v == "[":
                brackets += 1
            elif v == "]":
                brackets -= 1
            elif v == "{":
                braces += 1
            elif v == "}":
                braces -= 1
        top = parens <= 0 and brackets <= 0 and braces <= 0
        if not at_end and v == "=" and top and not assigned:
            # Binding side ends here for this declarator.
            names |= _collect_pattern_names(tokens, seg_start, i)
            assigned = True
        elif (at_end or (v == "," and top)):
            if not assigned:
                names |= _collect_pattern_names(tokens, seg_start, i)
            seg_start = i + 1
            assigned = False
        i += 1
    return names


def collect_exports(tokens: list[JSToken]) -> set[str]:
    """Return the set of named exports a module provides (default excluded)."""
    names: set[str] = set()
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        if tok.kind == "IDENT" and tok.value == "export" and not tok.template_expression:
            j = i + 1
            if j >= n:
                break
            nxt = tokens[j].value
            if nxt == "default":
                i = j + 1
                continue
            if nxt == "async":
                j += 1
                nxt = tokens[j].value if j < n else None
            if nxt == "function":
                k = j + 1
                if k < n and tokens[k].value == "*":
                    k += 1
                if k < n and tokens[k].kind == "IDENT" and tokens[k].value not in _JS_KEYWORDS:
                    names.add(tokens[k].value or "")
                i = j + 1
                continue
            if nxt == "class":
                if j + 1 < n and tokens[j + 1].kind == "IDENT":
                    names.add(tokens[j + 1].value or "")
                i = j + 1
                continue
            if nxt in {"const", "let", "var"}:
                end = _declaration_region_end(tokens, j + 1)
                names |= _collect_declarator_targets(tokens, j + 1, end)
                i = end
                continue
            if nxt == "{":
                close = _matching(tokens, j, "{", "}")
                if close is not None:
                    names |= _export_clause_names(tokens, j + 1, close)
                    i = close + 1
                    continue
            # `export * from '...'` and `export * as ns from '...'`
            if nxt == "*":
                if j + 2 < n and tokens[j + 1].value == "as" and tokens[j + 2].kind == "IDENT":
                    names.add(tokens[j + 2].value or "")
                i = j + 1
                continue
        i += 1
    return names


def _export_clause_names(tokens: list[JSToken], lo: int, hi: int) -> set[str]:
    """External names in `export { a, b as c }` → {a, c}."""
    names: set[str] = set()
    i = lo
    current: str | None = None
    while i < hi:
        t = tokens[i]
        if t.kind == "IDENT" and t.value not in {"as"}:
            current = t.value  # last ident before ',' wins (handles `a as c`)
        elif t.value == ",":
            if current:
                names.add(current)
            current = None
        i += 1
    if current:
        names.add(current)
    return names


def collect_free_calls(tokens: list[JSToken], bound: set[str]) -> list[tuple[str, int]]:
    """Return (name, line) for identifiers *called* but not bound locally.

    Only call positions (``foo(`` or ``new foo(``) are considered — the highest
    signal, lowest noise reference form. Member accesses, keys, and definitions
    are excluded.
    """
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    n = len(tokens)
    for i in range(n):
        tok = tokens[i]
        if tok.kind != "IDENT" or not tok.value:
            continue
        if tok.template_expression:
            continue
        name = tok.value
        if name in _JS_KEYWORDS or name in bound or name in seen:
            continue
        if i + 1 >= n or tokens[i + 1].value != "(":
            continue
        prev = tokens[i - 1].value if i else None
        # Member call (a.b()), definition (function f(), class method, get/set f()).
        if prev in {".", "?.", "function", "class", "get", "set"}:
            continue
        # Method / function definition: `name(...) {` — not a call.
        close = _matching(tokens, i + 1, "(", ")")
        if close is not None and close + 1 < n and tokens[close + 1].value == "{":
            continue
        out.append((name, tok.line))
        seen.add(name)
    return out


def build_export_index(modules: dict[str, str]) -> dict[str, set[str]]:
    """Map every exported symbol name to the set of modules that export it.

    *modules* maps a module's relative path to its source text.
    """
    index: dict[str, set[str]] = {}
    for rel, text in modules.items():
        try:
            tokens = lex_js(text)
        except Exception:  # pragma: no cover - lexer is defensive already
            continue
        for name in collect_exports(tokens):
            if name:
                index.setdefault(name, set()).add(rel)
    return index


def unresolved_calls(
    text: str,
    rel: str,
    export_index: dict[str, set[str]],
    extra_globals: frozenset[str] | set[str] = frozenset(),
) -> list[tuple[str, int, str]]:
    """Find calls in *rel* to symbols exported elsewhere but not imported here.

    Returns (name, line, source_module) tuples.
    """
    tokens = lex_js(text)
    bound = collect_bindings(tokens)
    results: list[tuple[str, int, str]] = []
    for name, line in collect_free_calls(tokens, bound):
        if name in _JS_GLOBALS or name in extra_globals:
            continue
        sources = export_index.get(name)
        if not sources:
            continue
        external = sorted(sources - {rel})
        if not external:
            continue
        results.append((name, line, external[0]))
    return results
