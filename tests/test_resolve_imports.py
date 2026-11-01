"""Tests for the resolve-imports check (used-but-not-imported symbols)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from drift.engine import RuleEngine
from drift.resolve import (
    build_export_index,
    collect_bindings,
    collect_exports,
    collect_free_calls,
    unresolved_calls,
)
from drift.analyze import lex_js

_MANIFEST = {"rules": [{"id": "resolve-imports", "type": "resolve-imports", "severity": "error"}]}


class ResolveUnitTests(unittest.TestCase):
    def test_collect_exports_named_and_function_and_class(self) -> None:
        src = (
            "export function hasUserMedia() {}\n"
            "export const FOO = 1, BAR = 2;\n"
            "export class Widget {}\n"
            "function priv() {}\n"
            "export { priv as publicPriv };\n"
            "export default function main() {}\n"
        )
        names = collect_exports(lex_js(src))
        self.assertIn("hasUserMedia", names)
        self.assertIn("FOO", names)
        self.assertIn("BAR", names)
        self.assertIn("Widget", names)
        self.assertIn("publicPriv", names)
        self.assertNotIn("main", names)  # default export is not a named import target
        self.assertNotIn("priv", names)  # exported under an alias

    def test_collect_reexport_from(self) -> None:
        src = "export { el, showToast } from './ui.js';\n"
        names = collect_exports(lex_js(src))
        self.assertEqual(names, {"el", "showToast"})

    def test_collect_bindings_imports_and_locals(self) -> None:
        src = (
            "import { a, b as c } from './x.js';\n"
            "import def, * as ns from './y.js';\n"
            "const { d, e: f } = obj;\n"
            "let g = 1;\n"
            "function fn(param1, { param2 }) { const local = 2; return local; }\n"
        )
        bound = collect_bindings(lex_js(src))
        for name in ["a", "c", "def", "ns", "d", "f", "g", "fn", "param1", "param2", "local"]:
            self.assertIn(name, bound, name)
        self.assertNotIn("b", bound)  # aliased away
        self.assertNotIn("e", bound)  # object key, not the binding

    def test_free_calls_detects_unbound_call(self) -> None:
        src = "function f() { return hasUserMedia() && other(); }\n"
        bound = collect_bindings(lex_js(src))
        frees = {name for name, _ in collect_free_calls(lex_js(src), bound)}
        self.assertIn("hasUserMedia", frees)
        self.assertIn("other", frees)

    def test_free_calls_ignores_member_and_definition(self) -> None:
        src = (
            "const obj = {};\n"
            "function method(x) { return x; }\n"
            "obj.method();\n"
            "method(1);\n"
        )
        bound = collect_bindings(lex_js(src))
        frees = {name for name, _ in collect_free_calls(lex_js(src), bound)}
        self.assertNotIn("method", frees)  # defined locally + member call


class ResolveEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_flags_used_but_not_imported_symbol(self) -> None:
        # The exact shape of the Waymark voice bug: hasUserMedia lives in
        # camera-capture.js, shared.js uses it but never imports it.
        self._write("camera-capture.js", "export function hasUserMedia() { return true; }\n")
        self._write(
            "shared.js",
            "import { captureStillFromCamera } from './camera-capture.js';\n"
            "export function isAudioRecordingSupported() { return hasUserMedia(); }\n",
        )
        violations = RuleEngine(_MANIFEST).scan(self.root, strict=True)
        self.assertTrue(
            any(v.rule_id == "resolve-imports" and v.restriction == "hasUserMedia" for v in violations),
            [v.message for v in violations],
        )

    def test_no_violation_when_imported(self) -> None:
        self._write("camera-capture.js", "export function hasUserMedia() { return true; }\n")
        self._write(
            "shared.js",
            "import { hasUserMedia } from './camera-capture.js';\n"
            "export function isAudioRecordingSupported() { return hasUserMedia(); }\n",
        )
        violations = RuleEngine(_MANIFEST).scan(self.root, strict=True)
        self.assertEqual([v for v in violations if v.rule_id == "resolve-imports"], [])

    def test_no_violation_for_locally_defined(self) -> None:
        self._write("a.js", "export function helper() {}\n")
        self._write(
            "b.js",
            "function helper() { return 1; }\n"
            "export function use() { return helper(); }\n",
        )
        violations = RuleEngine(_MANIFEST).scan(self.root, strict=True)
        self.assertEqual([v for v in violations if v.rule_id == "resolve-imports"], [])

    def test_no_violation_for_globals(self) -> None:
        self._write("a.js", "export function setTimeout() {}\n")  # shadows a global name
        self._write("b.js", "export function run() { return fetch('/x'); }\n")
        violations = RuleEngine(_MANIFEST).scan(self.root, strict=True)
        self.assertEqual([v for v in violations if v.rule_id == "resolve-imports"], [])

    def test_allow_globs_skip_files(self) -> None:
        self._write("camera.js", "export function hasUserMedia() {}\n")
        self._write("x.test.js", "export function t() { return hasUserMedia(); }\n")
        violations = RuleEngine(_MANIFEST).scan(self.root, strict=True)
        self.assertEqual([v for v in violations if v.rule_id == "resolve-imports"], [])

    def test_unresolved_calls_helper(self) -> None:
        idx = build_export_index({"m.js": "export function foo() {}\n"})
        res = unresolved_calls("export function g() { return foo(); }\n", "g.js", idx)
        self.assertEqual(res, [("foo", 1, "m.js")])


if __name__ == "__main__":
    unittest.main()
