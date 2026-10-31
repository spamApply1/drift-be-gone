"""Comprehensive unit and integration tests for the drift knowledge graph system."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from drift.cli import main
from drift.engine import Violation
from drift.graph import (
    Edge,
    Node,
    PropertyGraph,
    SubgraphPacker,
    analyze_cycles,
    build_knowledge_graph,
    compute_blast_radius,
    load_graph,
    save_graph,
    trace_layer_violations,
)


class PropertyGraphTests(unittest.TestCase):
    def test_node_and_edge_operations(self) -> None:
        g = PropertyGraph()
        n1 = g.add_node(Node(id="n1", kind="module", name="mod1", path="mod1.py"))
        n2 = g.add_node(Node(id="n2", kind="module", name="mod2", path="mod2.py"))
        e1 = g.add_edge(Edge(source="n1", target="n2", kind="IMPORTS"))

        self.assertEqual(g.get_node("n1"), n1)
        self.assertEqual(g.get_out_edges("n1"), [e1])
        self.assertEqual(g.get_in_edges("n2"), [e1])
        self.assertEqual([n.id for n in g.neighbors("n1", direction="out")], ["n2"])
        self.assertEqual([n.id for n in g.neighbors("n2", direction="in")], ["n1"])

    def test_shortest_path_and_all_paths(self) -> None:
        g = PropertyGraph()
        g.add_node(Node(id="a", kind="module", name="a"))
        g.add_node(Node(id="b", kind="module", name="b"))
        g.add_node(Node(id="c", kind="module", name="c"))
        g.add_edge(Edge(source="a", target="b", kind="IMPORTS"))
        g.add_edge(Edge(source="b", target="c", kind="IMPORTS"))
        g.add_edge(Edge(source="a", target="c", kind="IMPORTS"))

        shortest = g.shortest_path("a", "c")
        self.assertEqual(shortest, ["a", "c"])

        all_paths = g.all_paths("a", "c")
        self.assertEqual(len(all_paths), 2)
        self.assertIn(["a", "c"], all_paths)
        self.assertIn(["a", "b", "c"], all_paths)

    def test_serialization_roundtrip(self) -> None:
        g = PropertyGraph()
        g.add_node(Node(id="m1", kind="module", name="m1", properties={"loc": 42}))
        g.add_node(Node(id="f1", kind="function", name="f1"))
        g.add_edge(Edge(source="m1", target="f1", kind="DEFINES"))

        data = g.to_dict()
        g2 = PropertyGraph.from_dict(data)

        self.assertEqual(len(g2.nodes), 2)
        self.assertEqual(len(g2.edges), 1)
        self.assertEqual(g2.get_node("m1").properties["loc"], 42)

    def test_mermaid_generation(self) -> None:
        g = PropertyGraph()
        g.add_node(Node(id="a", kind="module", name="alpha"))
        g.add_node(Node(id="b", kind="module", name="beta"))
        g.add_edge(Edge(source="a", target="b", kind="IMPORTS"))
        mermaid = g.to_mermaid()
        self.assertIn("flowchart TD", mermaid)
        self.assertIn("alpha [module]", mermaid)
        self.assertIn("beta [module]", mermaid)


class KnowledgeGraphBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def test_extract_python_ast_and_calls(self) -> None:
        self._write(
            "core/engine.py",
            "class Engine:\n"
            "    def start(self):\n"
            "        self.run()\n"
            "    def run(self):\n"
            "        pass\n",
        )
        self._write(
            "main.py",
            "from core.engine import Engine\n"
            "def boot():\n"
            "    e = Engine()\n",
        )

        graph = build_knowledge_graph(self.root)
        self.assertIsNotNone(graph.get_node("module:core/engine.py"))
        self.assertIsNotNone(graph.get_node("class:core/engine.py:Engine"))
        self.assertIsNotNone(graph.get_node("method:core/engine.py:Engine.start"))
        self.assertIsNotNone(graph.get_node("fn:main.py:boot"))

        # Verify IMPORTS edge
        import_edges = graph.get_out_edges("module:main.py", kind="IMPORTS")
        self.assertTrue(any(e.target == "module:core/engine.py" for e in import_edges))

    def test_extract_js_ts(self) -> None:
        self._write(
            "src/utils.ts",
            "export function formatData(s: string): string { return s.trim(); }\n",
        )
        self._write(
            "src/app.ts",
            "import { formatData } from './utils';\n"
            "export const run = () => formatData('test');\n",
        )

        graph = build_knowledge_graph(self.root)
        self.assertIsNotNone(graph.get_node("module:src/utils.ts"))
        self.assertIsNotNone(graph.get_node("module:src/app.ts"))
        self.assertIsNotNone(graph.get_node("fn:src/utils.ts:formatData"))

        import_edges = graph.get_out_edges("module:src/app.ts", kind="IMPORTS")
        self.assertTrue(any(e.target == "module:src/utils.ts" for e in import_edges))

    def test_ui_bridge_extraction(self) -> None:
        self._write(
            "web/index.html",
            '<button data-action="submitForm" id="btn-submit">Submit</button>',
        )
        self._write(
            "web/app.js",
            "function submitForm() { alert('submitted'); }",
        )

        graph = build_knowledge_graph(self.root)
        self.assertIsNotNone(graph.get_node("action:submitForm"))
        self.assertIsNotNone(graph.get_node("element:web/index.html:btn-submit"))

        # Verify UI action dispatch edge to JS function
        dispatches = graph.get_out_edges("action:submitForm", kind="DISPATCHES_TO")
        self.assertTrue(any("submitForm" in e.target for e in dispatches))

    def test_workflow_extraction(self) -> None:
        self._write(".github/workflows/ci.yml", "name: CI\njobs:\n  test:\n    run: python3 -m unittest\n")
        self._write(".githooks/pre-commit", "#!/bin/sh\nexit 0\n")
        self._write(".gitignore", "*.pyc\n")
        self._write("README.md", "# Test Repo\n")

        graph = build_knowledge_graph(self.root)
        self.assertIsNotNone(graph.get_node("workflow:.github/workflows/ci.yml"))
        self.assertIsNotNone(graph.get_node("hook:.githooks/pre-commit"))
        self.assertIsNotNone(graph.get_node("config:.gitignore"))
        self.assertIsNotNone(graph.get_node("doc:README.md"))


class SolversTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def test_cycle_cutter_diagnostics_and_seams(self) -> None:
        self._write("a.py", "from b import helper_b\ndef run_a(): helper_b()\n")
        self._write("b.py", "from a import run_a\ndef helper_b(): pass\n")

        graph = build_knowledge_graph(self.root)
        cycles = analyze_cycles(graph)

        self.assertEqual(len(cycles), 1)
        diag = cycles[0]
        self.assertTrue(len(diag.cycle_chain) >= 2)
        self.assertTrue(len(diag.seam_candidates) > 0)

        # Check recommended seam
        top_seam = diag.seam_candidates[0]
        self.assertIn("extract_to_shared", [s.strategy for s in diag.seam_candidates])
        self.assertIsNotNone(top_seam.description)

    def test_layer_tracer(self) -> None:
        self._write("storage/db.py", "import ui.view\n")
        self._write("ui/view.py", "x = 1\n")

        graph = build_knowledge_graph(self.root)
        layers = [
            {"name": "ui", "paths": ["ui/*"]},
            {"name": "storage", "paths": ["storage/*"]},
        ]
        violations = trace_layer_violations(graph, layers)
        self.assertEqual(len(violations), 1)
        v = violations[0]
        self.assertEqual(v.source_layer, "storage")
        self.assertEqual(v.target_layer, "ui")
        self.assertIn("Invert dependency", v.remediation_suggestion)

    def test_blast_radius_computation(self) -> None:
        self._write("core/shared.py", "def util(): pass\n")
        self._write("feat1.py", "from core.shared import util\n")
        self._write("feat2.py", "from core.shared import util\n")

        graph = build_knowledge_graph(self.root)
        report = compute_blast_radius(graph, "core/shared.py")

        self.assertIsNotNone(report)
        self.assertEqual(report.target_path, "core/shared.py")
        self.assertGreaterEqual(report.afferent_coupling, 2)
        self.assertIn("feat1.py", report.impacted_files)
        self.assertIn("feat2.py", report.impacted_files)

    def test_subgraph_packer_and_llm_prompt(self) -> None:
        self._write("a.py", "import b\n")
        self._write("b.py", "import a\n")

        graph = build_knowledge_graph(self.root)
        packer = SubgraphPacker(graph)

        packed = packer.pack_neighborhood("a.py")
        self.assertIn("target", packed)
        self.assertIn("mermaid", packed)

        prompt = packer.generate_llm_remediation_prompt("a.py")
        self.assertIn("# Architectural Remediation Task for Coding Agent", prompt)
        self.assertIn("Circular Dependency Diagnostics", prompt)
        self.assertIn("Invariant Constraints", prompt)


class CLIGraphIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _write(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def test_cli_graph_build(self) -> None:
        self._write("main.py", "x = 1\n")
        out_json = self.root / "test_graph.json"
        code = main(["graph", "build", str(self.root), "--output", str(out_json)])
        self.assertEqual(code, 0)
        self.assertTrue(out_json.is_file())
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertIn("nodes", data)
        self.assertIn("edges", data)

    def test_cli_graph_blast_radius(self) -> None:
        self._write("utils.py", "def helper(): pass\n")
        code = main(["graph", "blast-radius", str(self.root), "utils.py", "--json"])
        self.assertEqual(code, 0)

    def test_cli_graph_explain(self) -> None:
        self._write("app.py", "y = 2\n")
        code = main(["graph", "explain", str(self.root), "app.py", "--mermaid"])
        self.assertEqual(code, 0)

    def test_cli_graph_prompt(self) -> None:
        self._write("cycle1.py", "import cycle2\n")
        self._write("cycle2.py", "import cycle1\n")
        code = main(["graph", "prompt", str(self.root), "cycle1.py"])
        self.assertEqual(code, 0)

    def test_cli_check_with_graph_diagnostics(self) -> None:
        self._write("cycle1.py", "import cycle2\n")
        self._write("cycle2.py", "import cycle1\n")
        code = main(["check", str(self.root), "--graph", "--json"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
