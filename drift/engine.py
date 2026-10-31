"""Architecture rule engine: enforce declared structure over the module graph."""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .analyze import Graph, Module, _strip_js_comments, build_graph

RULE_TYPES = frozenset(
    {
        "no-cycles",
        "layering",
        "forbidden-deps",
        "orphan-modules",
        "entrypoints-exist",
        "canonical-abstractions",
        "no-duplicate-symbols",
    }
)


@dataclass
class Violation:
    rule_id: str
    path: str
    line: int | None
    message: str
    severity: str = "error"
    restriction: str | None = None
    canonical: str | None = None


def _matches(path: str, patterns: list[str] | str) -> bool:
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(fnmatch(path, pattern) for pattern in patterns)


def _normalize_patterns(val: str | list[str] | None) -> list[str]:
    if not val:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


def _domain_of(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else ""


def _find_line_number(path: Path, pattern: str, is_regex: bool = False) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for idx, line in enumerate(lines, start=1):
        if is_regex:
            if re.search(pattern, line):
                return idx
        elif pattern in line:
            return idx
    return None


def _primitive_regex(primitive: str) -> re.Pattern:
    if "." in primitive:
        parts = primitive.split(".")
        sub = [rf"(?:\.\s*{re.escape(p)}|\[\s*['\"]{re.escape(p)}['\"]\s*\])" for p in parts[1:]]
        pattern = rf"\b{re.escape(parts[0])}" + "".join(sub) + r"(?!\w)"
    else:
        pattern = rf"\b{re.escape(primitive)}\b"
    return re.compile(pattern)


def _find_py_primitive_lines(code: str, primitive: str) -> list[int]:
    try:
        tokens = [
            t for t in tokenize.tokenize(io.BytesIO(code.encode("utf-8")).readline)
            if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    parts = primitive.split(".")
    lines: list[int] = []
    if len(parts) == 1:
        target = parts[0]
        for t in tokens:
            if t.type == tokenize.NAME and t.string == target:
                lines.append(t.start[0])
    else:
        target_len = len(parts) * 2 - 1
        expected: list[str] = []
        for i, p in enumerate(parts):
            if i > 0:
                expected.append(".")
            expected.append(p)
        for i in range(len(tokens) - target_len + 1):
            window = tokens[i : i + target_len]
            if all(window[j].string == exp for j, exp in enumerate(expected)):
                lines.append(window[0].start[0])
    return sorted(set(lines))


def _find_js_primitive_lines(code: str, primitive: str) -> list[int]:
    cleaned = _strip_js_comments(code)
    pattern = _primitive_regex(primitive)
    lines: list[int] = []
    for match in pattern.finditer(cleaned):
        lines.append(cleaned[:match.start()].count("\n") + 1)
    return sorted(set(lines))


def _find_cycles(graph: Graph) -> list[list[str]]:
    adjacency: dict[str, list[str]] = {node: [] for node in graph.modules}
    for src, dst in graph.edges:
        adjacency[src].append(dst)
    cycles: list[list[str]] = []
    color: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = 1
        stack.append(node)
        for neighbour in adjacency.get(node, []):
            state = color.get(neighbour, 0)
            if state == 0:
                visit(neighbour, stack)
            elif state == 1 and neighbour in stack:
                cycles.append(stack[stack.index(neighbour):] + [neighbour])
        stack.pop()
        color[node] = 2

    for node in graph.modules:
        if color.get(node, 0) == 0:
            visit(node, [])
    return cycles


def _no_cycles(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "no-cycles")
    out: list[Violation] = []
    seen: set[frozenset[str]] = set()
    for cycle in _find_cycles(graph):
        key = frozenset(cycle)
        if key in seen:
            continue
        seen.add(key)
        chain = " -> ".join(cycle)
        out.append(Violation(rule_id, cycle[0], None, f"circular dependency: {chain}"))
    return out


def _layering(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "layering")
    layers: list[dict] = rule.get("layers", [])
    rank = {}
    globs = {}
    for index, layer in enumerate(layers):
        name = layer.get("name", str(index))
        rank[name] = index
        globs[name] = layer.get("paths", [])

    def layer_of(path: str) -> str | None:
        for name, patterns in globs.items():
            if _matches(path, patterns):
                return name
        return None

    out: list[Violation] = []
    for src, dst in graph.edges:
        src_layer = layer_of(src)
        dst_layer = layer_of(dst)
        if src_layer is None or dst_layer is None:
            continue
        if rank[dst_layer] < rank[src_layer]:
            message = f"layer '{src_layer}' must not import lower layer '{dst_layer}' ({dst})"
            out.append(Violation(rule_id, src, None, message))
    return out


def _forbidden_deps(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "forbidden-deps")
    pairs: list[dict] = rule.get("pairs", [])
    out: list[Violation] = []
    for src, dst in graph.edges:
        for pair in pairs:
            if _matches(src, pair.get("from", [])) and _matches(dst, pair.get("to", [])):
                out.append(Violation(rule_id, src, None, f"forbidden dependency on {dst}"))
    return out


def _orphan_modules(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "orphan-modules")
    allow = rule.get("allow", ["**/__init__.py", "**/main.py", "**/cli.py", "**/*.test.*"])
    incoming = {node: 0 for node in graph.modules}
    for _, dst in graph.edges:
        incoming[dst] += 1
    out: list[Violation] = []
    for path, count in sorted(incoming.items()):
        if count == 0 and not _matches(path, allow):
            out.append(Violation(rule_id, path, None, "module is never imported (possible orphan)"))
    return out


def _entrypoints_exist(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "entrypoints-exist")
    out: list[Violation] = []
    for entry in rule.get("entrypoints", []):
        if not (root / entry).exists():
            out.append(Violation(rule_id, entry, None, "declared entrypoint is missing"))
    return out


def _check_restricted_imports(
    rule_id: str,
    rel: str,
    module: Module,
    file_path: Path,
    item: dict,
    canonical_display: str,
) -> list[Violation]:
    restricted_imports = item.get("restricted_imports", [])
    custom_message = item.get("message")
    out: list[Violation] = []
    seen: set[tuple[int, str]] = set()

    for imp, line_no in module.import_locations:
        for restricted in restricted_imports:
            if imp != restricted and not imp.startswith(f"{restricted}.") and not imp.startswith(f"{restricted}/"):
                continue
            if (line_no, restricted) in seen:
                continue
            seen.add((line_no, restricted))
            msg = custom_message or (
                f"direct import of '{restricted}' is restricted outside canonical path '{canonical_display}'. "
                f"Abstract into a reusable pattern in '{canonical_display}' and reuse it."
            )
            out.append(
                Violation(
                    rule_id=rule_id,
                    path=rel,
                    line=line_no,
                    message=msg,
                    restriction=restricted,
                    canonical=canonical_display,
                )
            )
            break
    return out


def _check_restricted_primitives(
    rule_id: str,
    rel: str,
    file_path: Path,
    item: dict,
    canonical_display: str,
) -> list[Violation]:
    restricted_primitives = item.get("restricted_primitives", [])
    custom_message = item.get("message")
    if not restricted_primitives or not file_path.exists():
        return []
    try:
        file_text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    out: list[Violation] = []
    is_py = rel.endswith(".py")
    for primitive in restricted_primitives:
        if is_py:
            lines = _find_py_primitive_lines(file_text, primitive)
        else:
            lines = _find_js_primitive_lines(file_text, primitive)
        for line_no in lines:
            msg = custom_message or (
                f"direct use of restricted primitive '{primitive}' is restricted outside "
                f"canonical path '{canonical_display}'. "
                f"Abstract into a reusable pattern in '{canonical_display}' and reuse it."
            )
            out.append(
                Violation(
                    rule_id=rule_id,
                    path=rel,
                    line=line_no,
                    message=msg,
                    restriction=primitive,
                    canonical=canonical_display,
                )
            )
    return out


def _check_restricted_symbols(
    rule_id: str,
    rel: str,
    module: Module,
    file_path: Path,
    item: dict,
    canonical_display: str,
) -> list[Violation]:
    restricted_symbols = item.get("restricted_symbols", [])
    custom_message = item.get("message")
    if not restricted_symbols:
        return []
    out: list[Violation] = []
    declared = set(module.functions + module.classes)
    for sym in restricted_symbols:
        if sym not in declared:
            continue
        line = _find_line_number(
            file_path,
            rf"\b(def|class|function|const|let|var)\s+{re.escape(sym)}\b",
            is_regex=True,
        )
        msg = custom_message or (
            f"symbol '{sym}' must only be defined in canonical path '{canonical_display}'."
        )
        out.append(
            Violation(
                rule_id=rule_id,
                path=rel,
                line=line,
                message=msg,
                restriction=sym,
                canonical=canonical_display,
            )
        )
    return out


def _scan_abstraction(
    rule_id: str,
    item: dict,
    graph: Graph,
    root: Path,
) -> list[Violation]:
    name = item.get("name") or item.get("id") or rule_id
    canonical_patterns = _normalize_patterns(item.get("canonical", []))
    canonical_display = ", ".join(canonical_patterns) if canonical_patterns else "(unspecified)"
    allow_patterns = _normalize_patterns(
        item.get("allow", ["tests/**", "**/*test*", "**/*_test.py", "**/*.test.*"])
    )

    out: list[Violation] = []
    if item.get("require_canonical_exists", False):
        if not any(_matches(p, canonical_patterns) for p in graph.modules):
            out.append(
                Violation(
                    rule_id=rule_id,
                    path=canonical_patterns[0] if canonical_patterns else ".",
                    line=None,
                    message=(
                        f"canonical abstraction '{name}' declared for '{canonical_display}', "
                        f"but no matching files exist on disk. Abstract the reusable pattern first."
                    ),
                    canonical=canonical_display,
                )
            )

    for rel, module in graph.modules.items():
        if _matches(rel, canonical_patterns) or _matches(rel, allow_patterns):
            continue
        file_path = root / rel
        out.extend(_check_restricted_imports(rule_id, rel, module, file_path, item, canonical_display))
        out.extend(_check_restricted_primitives(rule_id, rel, file_path, item, canonical_display))
        out.extend(_check_restricted_symbols(rule_id, rel, module, file_path, item, canonical_display))
    return out


def _normalize_abstractions(rule: dict) -> list[dict]:
    rule_canonical = rule.get("canonical")
    rule_allow = rule.get("allow")
    rule_message = rule.get("message")

    raw = rule.get("abstractions")
    if isinstance(raw, list):
        items: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            if "allow" not in merged and rule_allow is not None:
                merged["allow"] = rule_allow
            if "message" not in merged and rule_message is not None:
                merged["message"] = rule_message
            if "canonical" not in merged and rule_canonical is not None:
                merged["canonical"] = rule_canonical
            items.append(merged)
        return items
    if "canonical" in rule:
        return [rule]
    return []


def _canonical_abstractions(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "canonical-abstractions")
    abstractions = _normalize_abstractions(rule)
    out: list[Violation] = []
    for item in abstractions:
        out.extend(_scan_abstraction(rule_id, item, graph, root))
    return out


def _no_duplicate_symbols(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    rule_id = rule.get("id", "no-duplicate-symbols")
    default_allow = ["tests/**", "**/*test*", "**/*_test.py", "**/*.test.*", "**/__init__.py"]
    allow = _normalize_patterns(rule.get("allow", default_allow))
    default_ignore = [
        "main", "setup", "teardown", "run", "handler", "render", "default", "init",
        "Config", "Options", "Props", "State", "Client", "Context", "Error",
        "Response", "Request", "Data", "Params", "Result",
    ]
    ignore_symbols = set(rule.get("ignore", default_ignore))
    min_length = rule.get("min_length", 4)
    allow_same_domain = rule.get("allow_same_domain", True)

    symbols: dict[str, list[str]] = {}
    for rel, module in graph.modules.items():
        if _matches(rel, allow):
            continue
        for sym in set(module.functions + module.classes):
            if len(sym) >= min_length and sym not in ignore_symbols:
                symbols.setdefault(sym, []).append(rel)

    out: list[Violation] = []
    for sym, paths in sorted(symbols.items()):
        if len(paths) <= 1:
            continue
        if allow_same_domain and len({_domain_of(p) for p in paths}) <= 1:
            continue
        paths_str = ", ".join(sorted(paths))
        for path in sorted(paths):
            line = _find_line_number(
                root / path,
                rf"^(?:export\s+)?(?:async\s+)?(def|class|function|const|let|var)\s+{re.escape(sym)}\b",
                is_regex=True,
            ) or _find_line_number(root / path, sym)
            msg = (
                f"duplicate symbol '{sym}' defined in multiple modules: {paths_str}. "
                f"Abstract into a shared module and reuse."
            )
            out.append(Violation(rule_id=rule_id, path=path, line=line, message=msg, restriction=sym))
    return out


_RULES = {
    "no-cycles": _no_cycles,
    "layering": _layering,
    "forbidden-deps": _forbidden_deps,
    "orphan-modules": _orphan_modules,
    "entrypoints-exist": _entrypoints_exist,
    "canonical-abstractions": _canonical_abstractions,
    "no-duplicate-symbols": _no_duplicate_symbols,
}


class RuleEngine:
    def __init__(self, manifest: dict):
        self.rules = [r for r in manifest.get("rules", []) if r.get("enabled", True)]

    def scan(self, root: Path, strict: bool = False, graph: Graph | None = None) -> list[Violation]:
        graph = graph or build_graph(root)
        violations: list[Violation] = []
        for rule in self.rules:
            handler = _RULES.get(rule.get("type"))
            if handler is None:
                continue
            found = handler(rule, graph, root)
            severity = rule.get("severity", "error")
            for violation in found:
                violation.severity = severity
            violations.extend(found)
        if strict:
            return violations
        return [v for v in violations if v.severity == "error"]


def _validate_str_or_str_list(val: object, field_name: str, label: str, problems: list[str]) -> None:
    if val is None:
        return
    if isinstance(val, str):
        if not val.strip():
            problems.append(f"{label}: '{field_name}' must not be empty")
    elif isinstance(val, list):
        for idx, item in enumerate(val):
            if not isinstance(item, str) or not item.strip():
                problems.append(f"{label}: '{field_name}[{idx}]' must be a non-empty string")
    else:
        problems.append(f"{label}: '{field_name}' must be a string or list of strings")


def _validate_str_list(val: object, field_name: str, label: str, problems: list[str]) -> None:
    if val is None:
        return
    if not isinstance(val, list):
        problems.append(f"{label}: '{field_name}' must be a list of strings")
        return
    for idx, item in enumerate(val):
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{label}: '{field_name}[{idx}]' must be a non-empty string")


def _validate_abstraction_item(abs_item: object, a_label: str, parent_has_canonical: bool, problems: list[str]) -> None:
    if not isinstance(abs_item, dict):
        problems.append(f"{a_label}: abstraction must be an object")
        return
    if "canonical" not in abs_item and not parent_has_canonical:
        problems.append(f"{a_label}: must define 'canonical'")
    _validate_str_or_str_list(abs_item.get("canonical"), "canonical", a_label, problems)
    _validate_str_or_str_list(abs_item.get("allow"), "allow", a_label, problems)
    _validate_str_list(abs_item.get("restricted_imports"), "restricted_imports", a_label, problems)
    _validate_str_list(abs_item.get("restricted_primitives"), "restricted_primitives", a_label, problems)
    _validate_str_list(abs_item.get("restricted_symbols"), "restricted_symbols", a_label, problems)


def _validate_canonical_rule(rule: dict, label: str, problems: list[str]) -> None:
    abstractions = rule.get("abstractions")
    if "abstractions" in rule:
        if not isinstance(abstractions, list):
            problems.append(f"{label}: 'abstractions' must be an array")
            return
        for a_idx, abs_item in enumerate(abstractions):
            a_sub_label = f"{label}.abstractions[{a_idx}]"
            _validate_abstraction_item(abs_item, a_sub_label, "canonical" in rule, problems)
        return
    if "canonical" in rule:
        _validate_abstraction_item(rule, label, True, problems)
        return
    problems.append(f"{label}: must define 'canonical' or 'abstractions'")


def validate_manifest(manifest: dict) -> list[str]:
    problems: list[str] = []
    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["manifest must define a non-empty 'rules' array"]
    for index, rule in enumerate(rules):
        label = rule.get("id", f"rule[{index}]")
        rtype = rule.get("type")
        if rtype not in RULE_TYPES:
            problems.append(f"{label}: unknown rule type {rtype!r}")
        if rule.get("severity", "error") not in {"error", "warning"}:
            problems.append(f"{label}: severity must be error or warning")
        if rtype == "canonical-abstractions":
            _validate_canonical_rule(rule, label, problems)
    return problems
