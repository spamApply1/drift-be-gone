"""Architecture rule engine: enforce declared structure over the module graph."""

from __future__ import annotations

import io
import tokenize
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .analyze import Graph, JSToken, Module, build_graph, lex_js

RULE_TYPES = frozenset(
    {
        "no-cycles",
        "layering",
        "forbidden-deps",
        "orphan-modules",
        "entrypoints-exist",
        "canonical-abstractions",
        "no-duplicate-symbols",
        "no-duplicate-implementations",
        "require-synthetic",
        "no-duplicate-workflows",
        "verify-synthetic-alignment",
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
    parts = Path(path).parent.parts
    if not parts:
        return path
    if parts[0] in ("src", "lib", "app", "packages") and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _is_js_expression_end(token: JSToken) -> bool:
    if token.kind in {"NUMBER", "STRING", "TEMPLATE", "REGEX"}:
        return True
    if token.kind == "IDENT":
        return token.value not in {
            "await", "case", "delete", "else", "in", "instanceof", "new",
            "of", "return", "throw", "typeof", "void", "yield",
        }
    return token.value in {")", "]", "}", "++", "--"}


def _is_js_computed_property(tokens: list[JSToken], bracket: int) -> bool:
    if bracket >= 2 and tokens[bracket - 1].value == "?.":
        return _is_js_expression_end(tokens[bracket - 2])
    return bracket > 0 and _is_js_expression_end(tokens[bracket - 1])


def _match_js_primitive_path(tokens: list[JSToken], start: int, parts: list[str]) -> bool:
    index = start + 1
    for part in parts[1:]:
        if index >= len(tokens):
            return False
        if tokens[index].value in {".", "?."}:
            index += 1
            if index < len(tokens) and tokens[index].kind == "IDENT" and tokens[index].value == part:
                index += 1
            elif (
                tokens[index - 1].value == "?."
                and index + 2 < len(tokens)
                and tokens[index].value == "["
                and tokens[index + 1].kind == "STRING"
                and tokens[index + 1].value == part
                and tokens[index + 2].value == "]"
            ):
                index += 3
            else:
                return False
        elif (
            index + 2 < len(tokens)
            and tokens[index].value == "["
            and tokens[index + 1].kind == "STRING"
            and tokens[index + 1].value == part
            and tokens[index + 2].value == "]"
        ):
            index += 3
        else:
            return False
    return True


def _js_primitive_lines(tokens: list[JSToken], primitive: str) -> list[int]:
    parts = primitive.split(".")
    lines: set[int] = set()
    for index, token in enumerate(tokens):
        if token.kind == "IDENT" and token.value == parts[0]:
            if _match_js_primitive_path(tokens, index, parts):
                lines.add(token.line)
        if (
            len(parts) == 1
            and token.kind == "STRING"
            and token.value == primitive
            and index > 0
            and index + 1 < len(tokens)
            and tokens[index - 1].value == "["
            and tokens[index + 1].value == "]"
            and _is_js_computed_property(tokens, index - 1)
        ):
            lines.add(tokens[index - 1].line)
    return sorted(lines)


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
    return _js_primitive_lines(lex_js(code), primitive)


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
    restricted_imports = item.get("restricted_imports") or []
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
    restricted_primitives = item.get("restricted_primitives") or []
    custom_message = item.get("message")
    if not restricted_primitives or not file_path.exists():
        return []
    try:
        file_text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    out: list[Violation] = []
    is_py = rel.endswith(".py")
    js_tokens = None if is_py else lex_js(file_text)
    for primitive in restricted_primitives:
        if is_py:
            lines = _find_py_primitive_lines(file_text, primitive)
        else:
            lines = _js_primitive_lines(js_tokens, primitive)
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
    item: dict,
    canonical_display: str,
) -> list[Violation]:
    restricted_symbols = item.get("restricted_symbols") or []
    custom_message = item.get("message")
    if not restricted_symbols:
        return []
    out: list[Violation] = []
    declared = set(module.functions + module.classes)
    for sym in restricted_symbols:
        if sym not in declared:
            continue
        msg = custom_message or (
            f"symbol '{sym}' must only be defined in canonical path '{canonical_display}'."
        )
        lines = [line for name, line in module.symbol_locations if name == sym]
        for line in lines or [None]:
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
        out.extend(_check_restricted_symbols(rule_id, rel, module, item, canonical_display))
    return out


def _normalize_abstractions(rule: dict) -> list[dict]:
    rule_id = rule.get("id", "canonical-abstractions")
    inherited_fields = (
        "canonical",
        "allow",
        "message",
        "description",
        "restricted_imports",
        "restricted_primitives",
        "restricted_symbols",
        "require_canonical_exists",
    )

    raw = rule.get("abstractions")
    if isinstance(raw, list):
        items: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            for field_name in inherited_fields:
                if merged.get(field_name) is None and field_name in rule:
                    merged[field_name] = rule[field_name]
            if not merged.get("name") and not merged.get("id"):
                merged["name"] = rule_id
            items.append(merged)
        return items
    if "canonical" in rule:
        single = dict(rule)
        if not single.get("name") and not single.get("id"):
            single["name"] = rule_id
        return [single]
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
    allow_same_domain = rule.get("allow_same_domain", False)

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
            msg = (
                f"duplicate symbol '{sym}' defined in multiple modules: {paths_str}. "
                f"Abstract into a shared module and reuse."
            )
            module = graph.modules[path]
            line = next((ln for name, ln in module.symbol_locations if name == sym), None)
            out.append(Violation(rule_id=rule_id, path=path, line=line, message=msg, restriction=sym))
    return out


def _no_duplicate_implementations(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    from .graph import build_knowledge_graph, find_duplicate_implementations

    rule_id = rule.get("id", "no-duplicate-implementations")
    allow = _normalize_patterns(
        rule.get("allow", ["tests/**", "**/*test*", "**/*_test.py", "**/*.test.*"])
    )
    kg = build_knowledge_graph(root, include_violations=False)
    matches = find_duplicate_implementations(
        kg,
        min_similarity=rule.get("min_similarity", 0.86),
        min_features=rule.get("min_features", 8),
        ignore_paths=allow,
    )

    out: list[Violation] = []
    for match in matches:
        left, right = match.left, match.right
        for current, other in ((left, right), (right, left)):
            out.append(
                Violation(
                    rule_id=rule_id,
                    path=current["path"],
                    line=current["line"],
                    message=(
                        f"implementation shape is {match.similarity:.0%} similar to "
                        f"'{other['name']}' in {other['path']}:{other['line']}; "
                        "review whether both should use one shared abstraction"
                    ),
                    restriction="implementation-shape",
                )
            )
    return out


def _require_synthetic(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    from .synthetic import SyntheticWorkflowGovernance

    rule_id = rule.get("id", "require-synthetic")
    exempt = _normalize_patterns(rule.get("allow") or rule.get("exempt") or ["tests/**", "**/__init__.py", "drift/**"])
    gov = SyntheticWorkflowGovernance(
        root_dir=root,
        exempt_globs=tuple(exempt),
        require_synthetic=True,
        detect_duplicate_patterns=False,
    )
    violations, _ = gov.check_repository()
    return [
        Violation(
            rule_id=rule_id,
            path=v.file,
            line=None,
            message=v.message,
            severity=rule.get("severity", v.severity),
            restriction=v.workflow_id,
        )
        for v in violations
    ]


def _no_duplicate_workflows(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    from .synthetic import SyntheticWorkflowGovernance

    rule_id = rule.get("id", "no-duplicate-workflows")
    exempt = _normalize_patterns(rule.get("allow") or rule.get("exempt") or ["tests/**", "**/__init__.py", "drift/**"])
    min_sim = float(rule.get("min_similarity", 0.85))
    gov = SyntheticWorkflowGovernance(
        root_dir=root,
        exempt_globs=tuple(exempt),
        min_pattern_similarity=min_sim,
        require_synthetic=False,
        detect_duplicate_patterns=True,
    )
    violations, _ = gov.check_repository()
    return [
        Violation(
            rule_id=rule_id,
            path=v.file,
            line=None,
            message=v.message,
            severity=rule.get("severity", v.severity),
            restriction=v.workflow_id,
        )
        for v in violations
    ]


def _verify_synthetic_alignment(rule: dict, graph: Graph, root: Path) -> list[Violation]:
    from .synthetic import SyntheticWorkflowGovernance

    rule_id = rule.get("id", "verify-synthetic-alignment")
    exempt = _normalize_patterns(rule.get("allow") or rule.get("exempt") or ["tests/**", "**/__init__.py", "drift/**", "examples/**"])
    gov = SyntheticWorkflowGovernance(
        root_dir=root,
        exempt_globs=tuple(exempt),
        require_synthetic=False,
        detect_duplicate_patterns=False,
        verify_alignment=True,
    )
    violations, _ = gov.check_repository()
    return [
        Violation(
            rule_id=rule_id,
            path=v.file,
            line=None,
            message=v.message,
            severity=rule.get("severity", v.severity),
            restriction=v.workflow_id,
        )
        for v in violations
        if v.rule == "verify-synthetic-alignment"
    ]


_RULES = {
    "no-cycles": _no_cycles,
    "layering": _layering,
    "forbidden-deps": _forbidden_deps,
    "orphan-modules": _orphan_modules,
    "entrypoints-exist": _entrypoints_exist,
    "canonical-abstractions": _canonical_abstractions,
    "no-duplicate-symbols": _no_duplicate_symbols,
    "no-duplicate-implementations": _no_duplicate_implementations,
    "require-synthetic": _require_synthetic,
    "no-duplicate-workflows": _no_duplicate_workflows,
    "verify-synthetic-alignment": _verify_synthetic_alignment,
}


class RuleEngine:
    def __init__(self, manifest: dict):
        self.manifest = manifest if isinstance(manifest, dict) else {}
        rules = self.manifest.get("rules", [])
        self.rules = [r for r in rules if isinstance(r, dict) and r.get("enabled", True)] if isinstance(rules, list) else []

    def scan(self, root: Path, strict: bool = False, graph: Graph | None = None) -> list[Violation]:
        problems = validate_manifest(self.manifest)
        if problems:
            return [
                Violation(
                    rule_id="manifest-invalid",
                    path="manifest",
                    line=None,
                    message=f"invalid manifest: {p}",
                    severity="error",
                )
                for p in problems
            ]
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

        # Support top-level synthetic_workflows configuration
        if "synthetic_workflows" in self.manifest and isinstance(self.manifest["synthetic_workflows"], dict):
            sw_config = self.manifest["synthetic_workflows"]
            req_syn = sw_config.get("require_synthetic", True)
            det_dup = sw_config.get("detect_duplicate_patterns", True)
            ver_align = sw_config.get("verify_alignment", False)
            min_sim = float(sw_config.get("min_similarity", 0.85))
            exempt = _normalize_patterns(sw_config.get("exempt", ["tests/**", "drift/**", "examples/**", "**/__init__.py"]))

            has_req_rule = any(r.get("type") == "require-synthetic" for r in self.rules)
            has_dup_rule = any(r.get("type") == "no-duplicate-workflows" for r in self.rules)
            has_align_rule = any(r.get("type") == "verify-synthetic-alignment" for r in self.rules)

            run_req = req_syn and not has_req_rule
            run_dup = det_dup and not has_dup_rule
            run_align = ver_align and not has_align_rule

            if run_req or run_dup or run_align:
                from .synthetic import SyntheticWorkflowGovernance

                gov = SyntheticWorkflowGovernance(
                    root_dir=root,
                    exempt_globs=tuple(exempt),
                    min_pattern_similarity=min_sim,
                    require_synthetic=run_req,
                    detect_duplicate_patterns=run_dup,
                    verify_alignment=run_align,
                )
                syn_violations, _ = gov.check_repository()
                for sv in syn_violations:
                    violations.append(
                        Violation(
                            rule_id=sv.rule,
                            path=sv.file,
                            line=None,
                            message=sv.message,
                            severity=sv.severity,
                            restriction=sv.workflow_id,
                        )
                    )

        if strict:
            return violations
        return [v for v in violations if v.severity == "error"]


def _validate_str_or_str_list(val: object, field_name: str, label: str, problems: list[str]) -> None:
    if val is None:
        problems.append(f"{label}: '{field_name}' must not be null")
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
        problems.append(f"{label}: '{field_name}' must not be null")
        return
    if not isinstance(val, list):
        problems.append(f"{label}: '{field_name}' must be a list of strings")
        return
    for idx, item in enumerate(val):
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{label}: '{field_name}[{idx}]' must be a non-empty string")


def _validate_canonical_patterns(val: object, label: str, problems: list[str]) -> None:
    _validate_str_or_str_list(val, "canonical", label, problems)
    if isinstance(val, list) and not val:
        problems.append(f"{label}: 'canonical' must contain at least one path pattern")


def _has_abstraction_policy(item: dict, parent: dict | None = None) -> bool:
    fields = ("restricted_imports", "restricted_primitives", "restricted_symbols")
    for field_name in fields:
        value = item.get(field_name)
        if value is None and parent is not None:
            value = parent.get(field_name)
        if value:
            return True
    required = item.get("require_canonical_exists")
    if required is None and parent is not None:
        required = parent.get("require_canonical_exists")
    return required is True


def _validate_abstraction_item(
    abs_item: object, a_label: str, parent_has_canonical: bool, problems: list[str]
) -> None:
    if not isinstance(abs_item, dict):
        problems.append(f"{a_label}: abstraction must be an object")
        return
    if "canonical" not in abs_item and not parent_has_canonical:
        problems.append(f"{a_label}: must define 'canonical'")
    if "canonical" in abs_item:
        _validate_canonical_patterns(abs_item.get("canonical"), a_label, problems)
    if "allow" in abs_item:
        _validate_str_or_str_list(abs_item.get("allow"), "allow", a_label, problems)
    if "restricted_imports" in abs_item:
        _validate_str_list(abs_item.get("restricted_imports"), "restricted_imports", a_label, problems)
    if "restricted_primitives" in abs_item:
        _validate_str_list(abs_item.get("restricted_primitives"), "restricted_primitives", a_label, problems)
    if "restricted_symbols" in abs_item:
        _validate_str_list(abs_item.get("restricted_symbols"), "restricted_symbols", a_label, problems)
    if "message" in abs_item and (
        not isinstance(abs_item["message"], str) or not abs_item["message"].strip()
    ):
        problems.append(f"{a_label}: 'message' must be a non-empty string")
    for field_name in ("name", "id"):
        if field_name in abs_item and (
            not isinstance(abs_item[field_name], str) or not abs_item[field_name].strip()
        ):
            problems.append(f"{a_label}: '{field_name}' must be a non-empty string")
    if "description" in abs_item and not isinstance(abs_item["description"], str):
        problems.append(f"{a_label}: 'description' must be a string")
    if "require_canonical_exists" in abs_item and not isinstance(abs_item["require_canonical_exists"], bool):
        problems.append(f"{a_label}: 'require_canonical_exists' must be a boolean")


def _validate_canonical_rule(rule: dict, label: str, problems: list[str]) -> None:
    abstractions = rule.get("abstractions")
    if "abstractions" in rule:
        if not isinstance(abstractions, list):
            problems.append(f"{label}: 'abstractions' must be an array")
            return
        if not abstractions and rule.get("enabled", True):
            problems.append(f"{label}: enabled rule must define at least one abstraction")
        if "canonical" in rule:
            _validate_canonical_patterns(rule["canonical"], label, problems)
        if "allow" in rule:
            _validate_str_or_str_list(rule["allow"], "allow", label, problems)
        if "message" in rule and (not isinstance(rule["message"], str) or not rule["message"].strip()):
            problems.append(f"{label}: 'message' must be a non-empty string")
        for field_name in ("restricted_imports", "restricted_primitives", "restricted_symbols"):
            if field_name in rule:
                _validate_str_list(rule[field_name], field_name, label, problems)
        if "require_canonical_exists" in rule and not isinstance(rule["require_canonical_exists"], bool):
            problems.append(f"{label}: 'require_canonical_exists' must be a boolean")
        for a_idx, abs_item in enumerate(abstractions):
            a_sub_label = f"{label}.abstractions[{a_idx}]"
            _validate_abstraction_item(abs_item, a_sub_label, "canonical" in rule, problems)
            if (
                rule.get("enabled", True)
                and isinstance(abs_item, dict)
                and not _has_abstraction_policy(abs_item, rule)
            ):
                problems.append(f"{a_sub_label}: must define at least one restriction")
        return
    if "canonical" in rule:
        _validate_abstraction_item(rule, label, True, problems)
        if rule.get("enabled", True) and not _has_abstraction_policy(rule):
            problems.append(f"{label}: enabled rule must define at least one restriction")
        return
    problems.append(f"{label}: must define 'canonical' or 'abstractions'")


def validate_manifest(manifest: dict) -> list[str]:
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["manifest must define a non-empty 'rules' array"]

    if "synthetic_workflows" in manifest:
        sw = manifest["synthetic_workflows"]
        if not isinstance(sw, dict):
            problems.append("'synthetic_workflows' must be an object")
        else:
            if "require_synthetic" in sw and not isinstance(sw["require_synthetic"], bool):
                problems.append("'synthetic_workflows.require_synthetic' must be a boolean")
            if "detect_duplicate_patterns" in sw and not isinstance(sw["detect_duplicate_patterns"], bool):
                problems.append("'synthetic_workflows.detect_duplicate_patterns' must be a boolean")
            if "verify_alignment" in sw and not isinstance(sw["verify_alignment"], bool):
                problems.append("'synthetic_workflows.verify_alignment' must be a boolean")
            if "min_similarity" in sw:
                ms = sw["min_similarity"]
                if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not 0 < ms <= 1:
                    problems.append("'synthetic_workflows.min_similarity' must be a float between 0 and 1")
            if "exempt" in sw:
                _validate_str_list(sw["exempt"], "exempt", "synthetic_workflows", problems)

    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            problems.append(f"rule[{index}] must be an object")
            continue
        raw_label = rule.get("id")
        label = raw_label if isinstance(raw_label, str) and raw_label.strip() else f"rule[{index}]"
        if "id" in rule and (not isinstance(raw_label, str) or not raw_label.strip()):
            problems.append(f"rule[{index}]: 'id' must be a non-empty string")
        rtype = rule.get("type")
        if not isinstance(rtype, str) or rtype not in RULE_TYPES:
            problems.append(f"{label}: unknown rule type {rtype!r}")
        severity = rule.get("severity", "error")
        if not isinstance(severity, str) or severity not in {"error", "warning"}:
            problems.append(f"{label}: severity must be error or warning")
        if "enabled" in rule and not isinstance(rule["enabled"], bool):
            problems.append(f"{label}: 'enabled' must be a boolean")
        if "description" in rule and not isinstance(rule["description"], str):
            problems.append(f"{label}: 'description' must be a string")
        if rtype == "canonical-abstractions":
            _validate_canonical_rule(rule, label, problems)
        elif rtype == "no-duplicate-symbols":
            if "allow" in rule:
                _validate_str_or_str_list(rule["allow"], "allow", label, problems)
            if "ignore" in rule:
                _validate_str_list(rule["ignore"], "ignore", label, problems)
            min_length = rule.get("min_length", 4)
            if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 1:
                problems.append(f"{label}: 'min_length' must be a positive integer")
            if "allow_same_domain" in rule and not isinstance(rule["allow_same_domain"], bool):
                problems.append(f"{label}: 'allow_same_domain' must be a boolean")
        elif rtype == "no-duplicate-implementations":
            if "allow" in rule:
                _validate_str_or_str_list(rule["allow"], "allow", label, problems)
            min_similarity = rule.get("min_similarity", 0.86)
            if (
                isinstance(min_similarity, bool)
                or not isinstance(min_similarity, (int, float))
                or not 0 < min_similarity <= 1
            ):
                problems.append(f"{label}: 'min_similarity' must be greater than 0 and at most 1")
            min_features = rule.get("min_features", 8)
            if isinstance(min_features, bool) or not isinstance(min_features, int) or min_features < 1:
                problems.append(f"{label}: 'min_features' must be a positive integer")
        elif rtype == "require-synthetic":
            if "allow" in rule:
                _validate_str_or_str_list(rule["allow"], "allow", label, problems)
            if "exempt" in rule:
                _validate_str_or_str_list(rule["exempt"], "exempt", label, problems)
        elif rtype == "no-duplicate-workflows":
            if "allow" in rule:
                _validate_str_or_str_list(rule["allow"], "allow", label, problems)
            if "exempt" in rule:
                _validate_str_or_str_list(rule["exempt"], "exempt", label, problems)
            if "min_similarity" in rule:
                ms = rule["min_similarity"]
                if (
                    isinstance(ms, bool)
                    or not isinstance(ms, (int, float))
                    or not 0 < ms <= 1
                ):
                    problems.append(f"{label}: 'min_similarity' must be greater than 0 and at most 1")
        elif rtype == "verify-synthetic-alignment":
            if "allow" in rule:
                _validate_str_or_str_list(rule["allow"], "allow", label, problems)
            if "exempt" in rule:
                _validate_str_or_str_list(rule["exempt"], "exempt", label, problems)
    return problems
