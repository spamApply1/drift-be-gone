"""Synthetic companion file and user-journey workflow governance.

Enforces intentional behavioral documentation (.synthetic files) and detects
duplicate or near-duplicate user workflow sequences across the repository.
Also cross-checks declared .synthetic action-verb steps against AST/token reality
to prevent synthetic drift.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, List, Optional, Tuple


@dataclass
class WorkflowPattern:
    id: str
    user_intent: str
    trigger: str
    steps: List[str]
    outcome: str
    source_file: str
    synthetic_file: str
    allow_similar_workflows: List[str] = field(default_factory=list)

    @property
    def normalized_signature(self) -> Tuple[str, ...]:
        """Normalizes steps to action prefixes (e.g. 'validate:cart' -> 'validate')
        or keeps full canonical tokens for structural pattern matching."""
        return tuple(step.strip().lower() for step in self.steps if step.strip())


@dataclass
class WorkflowDuplicateMatch:
    wf_a: WorkflowPattern
    wf_b: WorkflowPattern
    similarity: float
    is_error: bool  # True for exact match or same trigger+outcome with >= 0.75 similarity
    message: str


@dataclass
class SyntheticViolation:
    rule: str
    severity: str  # "error" | "warning"
    file: str
    message: str
    related_file: Optional[str] = None
    workflow_id: Optional[str] = None


def _inspect_python_module(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {"text": text, "parse_error": True, "functions": [], "classes": [], "calls": [], "imports": [], "has_return": False, "has_mutation": False}

    functions: list[str] = []
    classes: list[str] = []
    calls: list[str] = []
    imports: list[str] = []
    has_return = False
    has_mutation = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                calls.append(func.id)
            elif isinstance(func, ast.Attribute):
                calls.append(func.attr)
                if isinstance(func.value, ast.Name):
                    calls.append(f"{func.value.id}.{func.attr}")
        elif isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)):
            if getattr(node, "value", None) is not None:
                has_return = True
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            has_mutation = True

    return {
        "text": text,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "imports": imports,
        "has_return": has_return,
        "has_mutation": has_mutation,
    }


def _inspect_jsts_module(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    calls = re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", text)
    imports = re.findall(r"""(?:from|require\s*\()\s*['"]([^'"]+)['"]""", text)
    functions = (
        re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", text)
        + re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", text)
    )
    classes = re.findall(r"\bclass\s+([A-Za-z_$][\w$]*)", text)
    has_return = bool(re.search(r"\breturn\b|\bdispatch\b", text))
    has_mutation = bool(re.search(r"\b=\s*[^=]", text))

    return {
        "text": text,
        "functions": functions,
        "classes": classes,
        "calls": calls,
        "imports": imports,
        "has_return": has_return,
        "has_mutation": has_mutation,
    }


class SyntheticWorkflowGovernance:
    REQUIRED_WORKFLOW_KEYS = {"id", "user_intent", "trigger", "steps", "outcome"}

    def __init__(
        self,
        root_dir: Path,
        code_extensions: Tuple[str, ...] = (".py", ".js", ".ts", ".tsx"),
        exempt_globs: Tuple[str, ...] = ("tests/**", "**/__init__.py", "drift/**", "examples/**"),
        min_pattern_similarity: float = 0.85,
        require_synthetic: bool = True,
        detect_duplicate_patterns: bool = True,
        verify_alignment: bool = False,
    ):
        self.root_dir = Path(root_dir)
        self.code_extensions = code_extensions
        self.exempt_globs = tuple(exempt_globs)
        self.min_pattern_similarity = min_pattern_similarity
        self.require_synthetic = require_synthetic
        self.detect_duplicate_patterns = detect_duplicate_patterns
        self.verify_alignment = verify_alignment

    def _is_exempt(self, rel_path: Path) -> bool:
        path_str = rel_path.as_posix()
        return any(
            rel_path.match(pattern)
            or fnmatch(path_str, pattern)
            or path_str.startswith("tests/")
            for pattern in self.exempt_globs
        )

    def _resolve_synthetic_path(self, code_file: Path) -> Optional[Path]:
        """Checks for `foo.py.synthetic` first, then `foo.synthetic`."""
        exact_sibling = code_file.with_name(f"{code_file.name}.synthetic")
        if exact_sibling.exists():
            return exact_sibling
        stem_sibling = code_file.with_suffix(".synthetic")
        if stem_sibling.exists():
            return stem_sibling
        return None

    def check_repository(self) -> Tuple[List[SyntheticViolation], List[WorkflowPattern]]:
        violations: List[SyntheticViolation] = []
        all_patterns: List[WorkflowPattern] = []

        # 1. Discover governed source files & verify .synthetic presence
        for path in sorted(self.root_dir.rglob("*")):
            if not path.is_file() or path.suffix not in self.code_extensions:
                continue
            try:
                rel_path = path.relative_to(self.root_dir)
            except ValueError:
                continue
            if self._is_exempt(rel_path):
                continue

            syn_path = self._resolve_synthetic_path(path)
            if syn_path is None:
                if self.require_synthetic:
                    violations.append(
                        SyntheticViolation(
                            rule="require-synthetic",
                            severity="error",
                            file=rel_path.as_posix(),
                            message=(
                                f"Missing '.synthetic' workflow definition for '{rel_path.as_posix()}'. "
                                f"Expected '{rel_path.name}.synthetic' defining user workflows."
                            ),
                        )
                    )
                continue

            # 2. Validate .synthetic schema & extract workflow patterns
            file_violations, patterns = self._parse_and_validate_synthetic(rel_path, syn_path)
            violations.extend(file_violations)
            all_patterns.extend(patterns)

        # 3. Detect duplicated user workflow patterns across the codebase
        if self.detect_duplicate_patterns:
            dup_violations = self._detect_duplicate_workflows(all_patterns)
            violations.extend(dup_violations)

        # 4. Verify AST/token alignment between code and declared .synthetic steps
        if self.verify_alignment:
            align_violations = self.verify_code_alignment(all_patterns)
            violations.extend(align_violations)

        return violations, all_patterns

    def _parse_and_validate_synthetic(
        self, rel_code_path: Path, syn_path: Path
    ) -> Tuple[List[SyntheticViolation], List[WorkflowPattern]]:
        violations: List[SyntheticViolation] = []
        patterns: List[WorkflowPattern] = []
        rel_syn_str = syn_path.relative_to(self.root_dir).as_posix()
        rel_code_str = rel_code_path.as_posix()

        try:
            data = json.loads(syn_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [
                SyntheticViolation(
                    rule="require-synthetic",
                    severity="error",
                    file=rel_syn_str,
                    message=f"Invalid JSON in '{rel_syn_str}': {exc}",
                )
            ], []

        if not isinstance(data, dict):
            return [
                SyntheticViolation(
                    rule="require-synthetic",
                    severity="error",
                    file=rel_syn_str,
                    message=f"Root of '{rel_syn_str}' must be a JSON object.",
                )
            ], []

        workflows = data.get("workflows")
        if not isinstance(workflows, list) or len(workflows) == 0:
            violations.append(
                SyntheticViolation(
                    rule="require-synthetic",
                    severity="error",
                    file=rel_syn_str,
                    message=f"'{rel_syn_str}' must define a non-empty 'workflows' list for '{rel_code_str}'.",
                )
            )
            return violations, patterns

        for idx, wf in enumerate(workflows):
            if not isinstance(wf, dict):
                violations.append(
                    SyntheticViolation(
                        rule="require-synthetic",
                        severity="error",
                        file=rel_syn_str,
                        message=f"Workflow #{idx + 1} in '{rel_syn_str}' must be an object.",
                    )
                )
                continue

            missing = self.REQUIRED_WORKFLOW_KEYS - set(wf.keys())
            if missing:
                violations.append(
                    SyntheticViolation(
                        rule="require-synthetic",
                        severity="error",
                        file=rel_syn_str,
                        message=f"Workflow #{idx + 1} in '{rel_syn_str}' missing keys: {sorted(missing)}",
                    )
                )
                continue

            if not isinstance(wf["steps"], list) or len(wf["steps"]) < 2:
                violations.append(
                    SyntheticViolation(
                        rule="require-synthetic",
                        severity="error",
                        file=rel_syn_str,
                        workflow_id=wf.get("id"),
                        message=f"Workflow '{wf.get('id')}' in '{rel_syn_str}' must have at least 2 user-journey steps.",
                    )
                )
                continue

            allow_similar = wf.get("allow_similar_workflows", [])
            if not isinstance(allow_similar, list) or any(not isinstance(item, str) for item in allow_similar):
                violations.append(
                    SyntheticViolation(
                        rule="require-synthetic",
                        severity="error",
                        file=rel_syn_str,
                        workflow_id=wf.get("id"),
                        message=f"Workflow '{wf.get('id')}' in '{rel_syn_str}': 'allow_similar_workflows' must be a list of workflow ID strings.",
                    )
                )
                continue

            patterns.append(
                WorkflowPattern(
                    id=str(wf["id"]).strip(),
                    user_intent=str(wf["user_intent"]).strip(),
                    trigger=str(wf["trigger"]).strip(),
                    steps=[str(s) for s in wf["steps"]],
                    outcome=str(wf["outcome"]).strip(),
                    source_file=rel_code_str,
                    synthetic_file=rel_syn_str,
                    allow_similar_workflows=[str(w).strip() for w in allow_similar if str(w).strip()],
                )
            )

        return violations, patterns

    def find_duplicate_pairs(
        self, patterns: List[WorkflowPattern]
    ) -> List[WorkflowDuplicateMatch]:
        """Find duplicate or near-duplicate workflow pairs across patterns."""
        matches: List[WorkflowDuplicateMatch] = []

        for i in range(len(patterns)):
            for j in range(i + 1, len(patterns)):
                wf_a = patterns[i]
                wf_b = patterns[j]

                # Skip if both files are part of the exact same explicit workflow ID
                # (multi-file participation in a single declared workflow is valid;
                # two DIFFERENT workflow IDs doing the same thing is sprawl/drift)
                if wf_a.id == wf_b.id and wf_a.source_file != wf_b.source_file:
                    continue

                sig_a = wf_a.normalized_signature
                sig_b = wf_b.normalized_signature

                # Compute sequence similarity on the behavioral step chains
                step_similarity = SequenceMatcher(None, sig_a, sig_b).ratio()
                same_trigger_and_outcome = (
                    wf_a.trigger.lower() == wf_b.trigger.lower()
                    and wf_a.outcome.lower() == wf_b.outcome.lower()
                )

                if sig_a == sig_b or (same_trigger_and_outcome and step_similarity >= 0.75):
                    msg = (
                        f"Duplicate user workflow pattern detected: '{wf_b.id}' in '{wf_b.source_file}' "
                        f"duplicates '{wf_a.id}' in '{wf_a.source_file}' "
                        f"(similarity: {step_similarity:.0%}, trigger: '{wf_b.trigger}', outcome: '{wf_b.outcome}'). "
                        f"Consolidate into a single canonical user flow."
                    )
                    matches.append(
                        WorkflowDuplicateMatch(
                            wf_a=wf_a,
                            wf_b=wf_b,
                            similarity=step_similarity,
                            is_error=True,
                            message=msg,
                        )
                    )
                elif step_similarity >= self.min_pattern_similarity:
                    # Declarative escape hatch: if workflow B explicitly acknowledges workflow A (or vice-versa),
                    # suppress the warning and duplicate graph edge
                    if wf_a.id in wf_b.allow_similar_workflows or wf_b.id in wf_a.allow_similar_workflows:
                        continue

                    msg = (
                        f"Near-duplicate workflow pattern ({step_similarity:.0%} match): "
                        f"'{wf_b.id}' ({wf_b.source_file}) closely mirrors '{wf_a.id}' ({wf_a.source_file})."
                    )
                    matches.append(
                        WorkflowDuplicateMatch(
                            wf_a=wf_a,
                            wf_b=wf_b,
                            similarity=step_similarity,
                            is_error=False,
                            message=msg,
                        )
                    )

        return matches

    def _detect_duplicate_workflows(
        self, patterns: List[WorkflowPattern]
    ) -> List[SyntheticViolation]:
        violations: List[SyntheticViolation] = []
        matches = self.find_duplicate_pairs(patterns)

        for match in matches:
            severity = "error" if match.is_error else "warning"
            violations.append(
                SyntheticViolation(
                    rule="no-duplicate-workflows",
                    severity=severity,
                    file=match.wf_b.source_file,
                    related_file=match.wf_a.source_file,
                    workflow_id=match.wf_b.id,
                    message=match.message,
                )
            )

        return violations

    def verify_code_alignment(
        self, patterns: List[WorkflowPattern]
    ) -> List[SyntheticViolation]:
        """Cross-check declared .synthetic action-verb steps against code AST and token reality."""
        violations: List[SyntheticViolation] = []

        external_keywords = {
            "requests", "urllib", "http", "https", "aiohttp", "httpx", "fetch", "axios",
            "socket", "rpc", "grpc", "boto3", "stripe", "redis", "sql", "db", "client",
            "api", "post", "get", "put", "delete", "connect", "charge", "query",
        }
        emit_keywords = {
            "emit", "dispatch", "publish", "trigger", "send", "notify", "broadcast",
            "fire", "post_message", "postmessage", "event", "logger", "log", "push",
        }

        # Cache file inspect results
        inspect_cache: dict[str, dict[str, Any]] = {}

        for wf in patterns:
            code_path = self.root_dir / wf.source_file
            if not code_path.is_file():
                continue

            rel_str = wf.source_file
            if rel_str not in inspect_cache:
                ext = code_path.suffix.lower()
                if ext == ".py":
                    inspect_cache[rel_str] = _inspect_python_module(code_path)
                elif ext in {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
                    inspect_cache[rel_str] = _inspect_jsts_module(code_path)
                else:
                    inspect_cache[rel_str] = {"functions": [], "classes": [], "calls": [], "imports": [], "has_return": False, "has_mutation": False}

            info = inspect_cache[rel_str]
            total_symbols = len(info.get("functions", [])) + len(info.get("classes", []))
            calls = set(str(c).lower() for c in info.get("calls", []))
            imports = set(str(i).lower() for i in info.get("imports", []))
            text = info.get("text", "").lower()

            # 1. Zero symbols or calls (Ce == 0) claiming multi-step orchestrator
            if total_symbols == 0 and len(calls) == 0 and not info.get("has_mutation") and len(wf.steps) >= 2:
                violations.append(
                    SyntheticViolation(
                        rule="verify-synthetic-alignment",
                        severity="warning",
                        file=wf.source_file,
                        workflow_id=wf.id,
                        message=(
                            f"Synthetic-code mismatch in '{wf.source_file}': module contains no executable symbols or outgoing calls, "
                            f"yet declares workflow '{wf.id}' with {len(wf.steps)} steps."
                        ),
                    )
                )
                continue

            # 2. Check external:* step declaration against network / I/O / client reality
            external_steps = [s for s in wf.steps if s.strip().lower().startswith("external:")]
            if external_steps:
                has_external = (
                    any(any(k in imp for k in external_keywords) for imp in imports)
                    or any(any(k in c for k in external_keywords) for c in calls)
                    or any(k in text for k in ("requests.", "http://", "https://", "fetch(", "axios.", "client."))
                )
                if not has_external:
                    for ext_step in external_steps:
                        violations.append(
                            SyntheticViolation(
                                rule="verify-synthetic-alignment",
                                severity="warning",
                                file=wf.source_file,
                                workflow_id=wf.id,
                                message=(
                                    f"Synthetic-code mismatch in '{wf.source_file}': workflow '{wf.id}' declares external step '{ext_step}', "
                                    f"but no network, I/O, or client call was found in the module."
                                ),
                            )
                        )

            # 3. Check emit:* step declaration against event dispatch / pub-sub / return reality
            emit_steps = [s for s in wf.steps if s.strip().lower().startswith("emit:")]
            if emit_steps:
                has_emit = (
                    any(any(k in c for k in emit_keywords) for c in calls)
                    or info.get("has_return", False)
                    or any(k in text for k in ("emit(", "dispatch(", "publish(", "events.", "emitter.", "logger."))
                )
                if not has_emit:
                    for emit_step in emit_steps:
                        violations.append(
                            SyntheticViolation(
                                rule="verify-synthetic-alignment",
                                severity="warning",
                                file=wf.source_file,
                                workflow_id=wf.id,
                                message=(
                                    f"Synthetic-code mismatch in '{wf.source_file}': workflow '{wf.id}' declares emit step '{emit_step}', "
                                    f"but no dispatch, emit, or event publication was found in the module."
                                ),
                            )
                        )

        return violations
