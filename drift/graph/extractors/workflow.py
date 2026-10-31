"""Workflow and repository infrastructure extractor for the knowledge graph.

Integrates chaos-be-gone workflow rails (.github/workflows, .githooks, readme, gitignore).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import Edge, Node, PropertyGraph

_RUN_CMD = re.compile(r"""\brun:\s*(.+)""")


class WorkflowExtractor:
    """Extracts workflow pipelines, hooks, and repo gates."""

    def __init__(self, root: Path, graph: PropertyGraph) -> None:
        self.root = root
        self.graph = graph

    def extract_repo_rails(self) -> None:
        # Check root README
        for name in ("README.md", "README.rst", "README.txt"):
            readme_path = self.root / name
            if readme_path.is_file():
                self.graph.add_node(
                    Node(
                        id=f"doc:{name}",
                        kind="documentation",
                        name=name,
                        path=name,
                        line=1,
                    )
                )
                break

        # Check .gitignore
        if (self.root / ".gitignore").is_file():
            self.graph.add_node(
                Node(
                    id="config:.gitignore",
                    kind="config",
                    name=".gitignore",
                    path=".gitignore",
                    line=1,
                )
            )

        # Check .githooks
        githooks_dir = self.root / ".githooks"
        if githooks_dir.is_dir():
            for hook in githooks_dir.iterdir():
                if hook.is_file():
                    rel = hook.relative_to(self.root).as_posix()
                    self.graph.add_node(
                        Node(
                            id=f"hook:{rel}",
                            kind="githook",
                            name=hook.name,
                            path=rel,
                            line=1,
                        )
                    )

        # Check .github/workflows
        wf_dir = self.root / ".github" / "workflows"
        if wf_dir.is_dir():
            for wf in wf_dir.glob("*.y*ml"):
                rel = wf.relative_to(self.root).as_posix()
                wf_id = f"workflow:{rel}"
                content = wf.read_text(encoding="utf-8", errors="ignore")
                self.graph.add_node(
                    Node(
                        id=wf_id,
                        kind="workflow",
                        name=wf.name,
                        path=rel,
                        line=1,
                    )
                )

                # Link runs to scripts/entrypoints
                for match in _RUN_CMD.finditer(content):
                    cmd = match.group(1).strip()
                    for node in self.graph.find_nodes(kind="module"):
                        if node.path and node.path in cmd:
                            self.graph.add_edge(
                                Edge(source=wf_id, target=node.id, kind="EXECUTES", properties={"command": cmd})
                            )

    def extract_synthetic_workflows(self) -> None:
        try:
            from ...synthetic import SyntheticWorkflowGovernance
        except (ImportError, ValueError):
            from drift.synthetic import SyntheticWorkflowGovernance

        gov = SyntheticWorkflowGovernance(
            root_dir=self.root,
            require_synthetic=False,
            detect_duplicate_patterns=True,
        )
        _, patterns = gov.check_repository()

        for wf in patterns:
            wf_node_id = f"workflow:{wf.id}"
            if wf_node_id not in self.graph.nodes:
                self.graph.add_node(
                    Node(
                        id=wf_node_id,
                        kind="user_workflow",
                        name=wf.id,
                        path=wf.synthetic_file,
                        line=1,
                        properties={
                            "intent": wf.user_intent,
                            "trigger": wf.trigger,
                            "steps": wf.steps,
                            "outcome": wf.outcome,
                            "source_file": wf.source_file,
                            "synthetic_file": wf.synthetic_file,
                        },
                    )
                )

            mod_id = f"module:{wf.source_file}"
            self.graph.add_edge(
                Edge(
                    source=mod_id,
                    target=wf_node_id,
                    kind="PARTICIPATES_IN_WORKFLOW",
                )
            )

        matches = gov.find_duplicate_pairs(patterns)
        for match in matches:
            self.graph.add_edge(
                Edge(
                    source=f"workflow:{match.wf_b.id}",
                    target=f"workflow:{match.wf_a.id}",
                    kind="DUPLICATES_WORKFLOW_PATTERN",
                    properties={
                        "weight": match.similarity,
                        "similarity": match.similarity,
                        "source_b": match.wf_b.source_file,
                        "source_a": match.wf_a.source_file,
                        "is_error": match.is_error,
                    },
                )
            )
