"""Tests for drift-be-gone: graph analysis, architecture rules, and the map."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from drift.analyze import build_graph
from drift.cli import main
from drift.engine import RuleEngine, validate_manifest
from drift.mapper import build_map


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_python_imports_become_edges(self) -> None:
        self._write("pkg/__init__.py", "")
        self._write("pkg/a.py", "from pkg import b\n")
        self._write("pkg/b.py", "x = 1\n")
        graph = build_graph(self.root)
        self.assertIn(("pkg/a.py", "pkg/b.py"), graph.edges)

    def test_symbols_collected(self) -> None:
        self._write("m.py", "def foo():\n    return 1\n\n\nclass Bar:\n    pass\n")
        graph = build_graph(self.root)
        module = graph.modules["m.py"]
        self.assertIn("foo", module.functions)
        self.assertIn("Bar", module.classes)


class ArchitectureRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_cycle_detected(self) -> None:
        self._write("a.py", "import b\n")
        self._write("b.py", "import a\n")
        manifest = {"rules": [{"id": "no-cycles", "type": "no-cycles", "severity": "error"}]}
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertTrue(any(v.rule_id == "no-cycles" for v in violations))

    def test_no_cycle_passes(self) -> None:
        self._write("a.py", "import b\n")
        self._write("b.py", "x = 1\n")
        manifest = {"rules": [{"id": "no-cycles", "type": "no-cycles", "severity": "error"}]}
        self.assertEqual(RuleEngine(manifest).scan(self.root, strict=True), [])

    def test_layering_violation(self) -> None:
        self._write("core/base.py", "import ui.page\n")
        self._write("ui/page.py", "x = 1\n")
        manifest = {
            "rules": [
                {
                    "id": "layering",
                    "type": "layering",
                    "severity": "error",
                    "layers": [
                        {"name": "ui", "paths": ["ui/*"]},
                        {"name": "core", "paths": ["core/*"]},
                    ],
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertTrue(any(v.rule_id == "layering" for v in violations))

    def test_forbidden_dependency(self) -> None:
        self._write("domain/model.py", "import web.view\n")
        self._write("web/view.py", "x = 1\n")
        manifest = {
            "rules": [
                {
                    "id": "forbidden-deps",
                    "type": "forbidden-deps",
                    "severity": "error",
                    "pairs": [{"from": ["domain/*"], "to": ["web/*"]}],
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertTrue(any(v.rule_id == "forbidden-deps" for v in violations))

    def test_entrypoint_missing(self) -> None:
        manifest = {
            "rules": [
                {
                    "id": "entrypoints-exist",
                    "type": "entrypoints-exist",
                    "severity": "error",
                    "entrypoints": ["main.py"],
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertTrue(any(v.rule_id == "entrypoints-exist" for v in violations))


class MapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_map_reports_nodes_edges_domains(self) -> None:
        self._write("pkg/a.py", "from pkg import b\n")
        self._write("pkg/b.py", "def f():\n    return 1\n")
        artifact = build_map(self.root)
        self.assertIn("pkg/a.py", artifact["graph_nodes"])
        self.assertIn(["pkg/a.py", "pkg/b.py"], artifact["graph_edges"])
        self.assertIn("pkg", artifact["inferred_domains"])
        self.assertTrue(artifact["synthetic_brief"])


class ManifestAndCliTests(unittest.TestCase):
    def test_valid_default_manifest(self) -> None:
        manifest = json.loads(
            (Path(__file__).resolve().parent.parent / "drift" / "data" / "default_manifest.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(validate_manifest(manifest), [])

    def test_unknown_type_rejected(self) -> None:
        self.assertTrue(validate_manifest({"rules": [{"id": "x", "type": "bogus"}]}))

    def test_cli_check_clean_tree(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(main(["check", str(root), "--json"]), 0)

    def test_cli_map_writes_output(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        out = root / "map.json"
        self.assertEqual(main(["map", str(root), "--output", str(out)]), 0)
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
