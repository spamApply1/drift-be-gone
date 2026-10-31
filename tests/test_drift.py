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

    def test_canonical_abstraction_blocks_restricted_import(self) -> None:
        self._write("shared/rtc/client.py", "import aiortc\n")
        self._write("features/call.py", "import aiortc\n")
        manifest = {
            "rules": [
                {
                    "id": "canonical-webrtc",
                    "type": "canonical-abstractions",
                    "severity": "error",
                    "canonical": "shared/rtc/**",
                    "restricted_imports": ["aiortc"],
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path, "features/call.py")
        self.assertEqual(violations[0].line, 1)
        self.assertIn("aiortc", violations[0].message)

    def test_canonical_abstraction_allows_canonical_and_test_usage(self) -> None:
        self._write("shared/rtc/client.py", "import aiortc\n")
        self._write("tests/test_call.py", "import aiortc\n")
        manifest = {
            "rules": [
                {
                    "id": "canonical-webrtc",
                    "type": "canonical-abstractions",
                    "severity": "error",
                    "canonical": "shared/rtc/**",
                    "restricted_imports": ["aiortc"],
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertEqual(violations, [])

    def test_canonical_abstraction_blocks_restricted_primitives(self) -> None:
        self._write("shared/rtc/webrtc.ts", "const pc = new RTCPeerConnection();\n")
        self._write("features/chat.ts", "// chat tool\nconst pc = new RTCPeerConnection();\n")
        manifest = {
            "rules": [
                {
                    "id": "canonical-webrtc",
                    "type": "canonical-abstractions",
                    "severity": "error",
                    "canonical": "shared/rtc/**",
                    "restricted_primitives": ["RTCPeerConnection"],
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path, "features/chat.ts")
        self.assertEqual(violations[0].line, 2)
        self.assertIn("RTCPeerConnection", violations[0].message)

    def test_canonical_abstraction_require_canonical_exists(self) -> None:
        self._write("features/chat.py", "x = 1\n")
        manifest = {
            "rules": [
                {
                    "id": "canonical-webrtc",
                    "type": "canonical-abstractions",
                    "severity": "error",
                    "canonical": "shared/rtc/**",
                    "require_canonical_exists": True,
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertTrue(any("no matching files exist on disk" in v.message for v in violations))

    def test_no_duplicate_symbols_detected(self) -> None:
        self._write("tools/chat.py", "class WebRTCChat:\n    pass\n")
        self._write("app/video.py", "class WebRTCChat:\n    pass\n")
        manifest = {
            "rules": [
                {
                    "id": "no-duplicate-symbols",
                    "type": "no-duplicate-symbols",
                    "severity": "error",
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertEqual(len(violations), 2)
        paths = {v.path for v in violations}
        self.assertEqual(paths, {"tools/chat.py", "app/video.py"})
        self.assertTrue(all(v.line == 1 for v in violations))
        self.assertTrue(all("WebRTCChat" in v.message for v in violations))

    def test_no_duplicate_symbols_ignored_for_tests(self) -> None:
        self._write("tools/chat.py", "class WebRTCChat:\n    pass\n")
        self._write("tests/test_chat.py", "class WebRTCChat:\n    pass\n")
        manifest = {
            "rules": [
                {
                    "id": "no-duplicate-symbols",
                    "type": "no-duplicate-symbols",
                    "severity": "error",
                }
            ]
        }
        violations = RuleEngine(manifest).scan(self.root, strict=True)
        self.assertEqual(violations, [])


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

    def test_map_includes_canonical_abstractions(self) -> None:
        self._write("pkg/a.py", "x = 1\n")
        manifest = {
            "rules": [
                {
                    "id": "canonical-webrtc",
                    "type": "canonical-abstractions",
                    "canonical": "shared/rtc/**",
                    "restricted_primitives": ["RTCPeerConnection"],
                }
            ]
        }
        artifact = build_map(self.root, manifest=manifest)
        self.assertIn("canonical_abstractions", artifact)
        self.assertEqual(len(artifact["canonical_abstractions"]), 1)
        self.assertEqual(artifact["canonical_abstractions"][0]["name"], "canonical-webrtc")
        self.assertEqual(artifact["canonical_abstractions"][0]["canonical"], "shared/rtc/**")


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
