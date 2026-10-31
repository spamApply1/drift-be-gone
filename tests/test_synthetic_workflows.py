"""Tests for .synthetic companion files and workflow governance."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    import pytest
except ImportError:
    pytest = None

from drift.engine import RuleEngine
from drift.graph import SubgraphPacker, build_knowledge_graph
from drift.mapper import build_map
from drift.synthetic import SyntheticWorkflowGovernance


def write_code_and_synthetic(
    base_dir: Path,
    rel_code_path: str,
    code_content: str = "def handler(): pass\n",
    synthetic_payload: dict | None = None,
):
    code_path = base_dir / rel_code_path
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text(code_content, encoding="utf-8")

    if synthetic_payload is not None:
        syn_path = code_path.with_name(f"{code_path.name}.synthetic")
        syn_path.write_text(json.dumps(synthetic_payload, indent=2), encoding="utf-8")


def test_missing_synthetic_file_is_flagged(tmp_path: Path):
    """Every non-exempt code file must have a .synthetic companion file."""
    write_code_and_synthetic(
        tmp_path,
        "app/orders/create.py",
        synthetic_payload=None,  # Missing .synthetic!
    )

    gov = SyntheticWorkflowGovernance(root_dir=tmp_path)
    violations, _ = gov.check_repository()

    assert len(violations) == 1
    assert violations[0].rule == "require-synthetic"
    assert violations[0].file == "app/orders/create.py"
    assert "Missing '.synthetic' workflow definition" in violations[0].message


def test_test_files_are_exempt_from_synthetic_requirement(tmp_path: Path):
    """Files in tests/** should not require .synthetic files."""
    write_code_and_synthetic(
        tmp_path,
        "tests/test_orders.py",
        synthetic_payload=None,
    )

    gov = SyntheticWorkflowGovernance(root_dir=tmp_path)
    violations, _ = gov.check_repository()
    assert violations == []


def test_multi_file_participation_in_same_workflow_passes(tmp_path: Path):
    """Multiple files participating in the same canonical workflow ID should pass."""
    shared_workflow = {
        "workflows": [
            {
                "id": "user-checkout-flow",
                "user_intent": "User purchases items in their cart",
                "trigger": "ui.click:checkout_button",
                "steps": [
                    "validate:cart_items",
                    "verify:user_session",
                    "external:charge_payment",
                    "mutate:order_status:paid",
                    "emit:order_confirmation",
                ],
                "outcome": "order_completed",
            }
        ]
    }

    write_code_and_synthetic(tmp_path, "app/ui/checkout_btn.ts", synthetic_payload=shared_workflow)
    write_code_and_synthetic(tmp_path, "app/services/checkout_service.py", synthetic_payload=shared_workflow)

    gov = SyntheticWorkflowGovernance(root_dir=tmp_path)
    violations, patterns = gov.check_repository()

    assert violations == []
    assert len(patterns) == 2


def test_duplicate_user_workflow_pattern_is_flagged(tmp_path: Path):
    """Flags when a second module invents a parallel user workflow with the same behavioral pattern."""
    # Canonical flow in checkout module
    write_code_and_synthetic(
        tmp_path,
        "app/checkout/standard_checkout.py",
        synthetic_payload={
            "workflows": [
                {
                    "id": "standard-cart-checkout",
                    "user_intent": "User completes purchase from shopping cart",
                    "trigger": "ui.submit:payment_form",
                    "steps": [
                        "validate:payment_input",
                        "verify:auth_token",
                        "mutate:inventory_reserve",
                        "external:stripe_charge",
                        "mutate:order_record:paid",
                        "emit:email_receipt",
                    ],
                    "outcome": "purchase_complete",
                }
            ]
        },
    )

    # Second developer builds a nearly identical "one-click buy" flow in another module
    write_code_and_synthetic(
        tmp_path,
        "app/promos/flash_sale_buy.py",
        synthetic_payload={
            "workflows": [
                {
                    "id": "flash-sale-instant-buy",
                    "user_intent": "User buys item from flash sale banner",
                    "trigger": "ui.submit:payment_form",
                    "steps": [
                        "validate:payment_input",
                        "verify:auth_token",
                        "mutate:inventory_reserve",
                        "external:stripe_charge",
                        "mutate:order_record:paid",
                        "emit:sms_receipt",  # Only 1 step differs out of 6 (~83-90% match + same trigger/outcome)
                    ],
                    "outcome": "purchase_complete",
                }
            ]
        },
    )

    gov = SyntheticWorkflowGovernance(root_dir=tmp_path, min_pattern_similarity=0.80)
    violations, _ = gov.check_repository()

    dup_violations = [v for v in violations if v.rule == "no-duplicate-workflows"]
    assert len(dup_violations) == 1
    v = dup_violations[0]
    assert v.file == "app/promos/flash_sale_buy.py"
    assert v.related_file == "app/checkout/standard_checkout.py"
    assert "flash-sale-instant-buy" in v.message
    assert "standard-cart-checkout" in v.message


class TestSyntheticWorkflows(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_missing_synthetic_file_is_flagged(self) -> None:
        test_missing_synthetic_file_is_flagged(self.tmp_path)

    def test_test_files_are_exempt_from_synthetic_requirement(self) -> None:
        test_test_files_are_exempt_from_synthetic_requirement(self.tmp_path)

    def test_multi_file_participation_in_same_workflow_passes(self) -> None:
        test_multi_file_participation_in_same_workflow_passes(self.tmp_path)

    def test_duplicate_user_workflow_pattern_is_flagged(self) -> None:
        test_duplicate_user_workflow_pattern_is_flagged(self.tmp_path)

    def test_malformed_synthetic_json_is_flagged(self) -> None:
        code_file = self.tmp_path / "app/bad.py"
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.write_text("x = 1\n", encoding="utf-8")
        syn_file = self.tmp_path / "app/bad.py.synthetic"
        syn_file.write_text("NOT JSON", encoding="utf-8")

        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path)
        violations, _ = gov.check_repository()
        self.assertTrue(any("Invalid JSON" in v.message for v in violations))

    def test_missing_workflow_keys_is_flagged(self) -> None:
        write_code_and_synthetic(
            self.tmp_path,
            "app/service.py",
            synthetic_payload={"workflows": [{"id": "incomplete-flow"}]},
        )
        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path)
        violations, _ = gov.check_repository()
        self.assertTrue(any("missing keys" in v.message for v in violations))

    def test_rule_engine_manifest_synthetic_workflows(self) -> None:
        write_code_and_synthetic(
            self.tmp_path,
            "app/orders.py",
            synthetic_payload=None,
        )
        manifest = {
            "version": 1,
            "synthetic_workflows": {
                "require_synthetic": True,
                "detect_duplicate_patterns": True,
                "exempt": ["tests/**"],
            },
            "rules": [
                {
                    "id": "no-cycles",
                    "type": "no-cycles",
                    "enabled": True,
                    "severity": "error",
                }
            ],
        }
        violations = RuleEngine(manifest).scan(self.tmp_path, strict=True)
        self.assertTrue(any(v.rule_id == "require-synthetic" and v.path == "app/orders.py" for v in violations))

    def test_drift_map_exports_canonical_workflows(self) -> None:
        workflow_data = {
            "workflows": [
                {
                    "id": "user-login-flow",
                    "user_intent": "User authenticates into account",
                    "trigger": "ui.submit:login_form",
                    "steps": ["validate:credentials", "verify:session", "mutate:auth_state"],
                    "outcome": "user_logged_in",
                }
            ]
        }
        write_code_and_synthetic(self.tmp_path, "src/auth/login.py", synthetic_payload=workflow_data)
        write_code_and_synthetic(self.tmp_path, "src/auth/session.py", synthetic_payload=workflow_data)

        artifact = build_map(self.tmp_path)
        self.assertIn("canonical_workflows", artifact)
        wfs = artifact["canonical_workflows"]
        self.assertEqual(len(wfs), 1)
        self.assertEqual(wfs[0]["id"], "user-login-flow")
        self.assertEqual(len(wfs[0]["participating_files"]), 2)
        self.assertIn("src/auth/login.py", wfs[0]["participating_files"])
        self.assertIn("src/auth/session.py", wfs[0]["participating_files"])

    def test_graph_and_llm_prompt_workflow_remediation(self) -> None:
        # Standard checkout
        write_code_and_synthetic(
            self.tmp_path,
            "src/checkout/payment.py",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "submit-order-payment",
                        "user_intent": "User submits payment details to complete an order",
                        "trigger": "ui.click:checkout_submit_btn",
                        "steps": [
                            "validate:user_input",
                            "verify:auth_session",
                            "mutate:order_state:pending",
                            "external:payment_gateway_charge",
                            "mutate:order_state:completed",
                            "emit:notification_receipt",
                        ],
                        "outcome": "order_paid_and_confirmed",
                    }
                ]
            },
        )

        # Quick pay in billing with parallel flow
        write_code_and_synthetic(
            self.tmp_path,
            "src/billing/quick_pay.py",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "quick-pay-checkout",
                        "user_intent": "User one-click buys item",
                        "trigger": "ui.click:checkout_submit_btn",
                        "steps": [
                            "validate:user_input",
                            "verify:auth_session",
                            "mutate:order_state:pending",
                            "external:payment_gateway_charge",
                            "mutate:order_state:completed",
                            "emit:notification_sms",
                        ],
                        "outcome": "order_paid_and_confirmed",
                    }
                ]
            },
        )

        graph = build_knowledge_graph(self.tmp_path, include_violations=False)

        # Verify workflow nodes and edges exist
        wf_nodes = graph.find_nodes(kind="user_workflow")
        self.assertEqual(len(wf_nodes), 2)

        participates_edges = [e for e in graph.edges if e.kind == "PARTICIPATES_IN_WORKFLOW"]
        self.assertEqual(len(participates_edges), 2)

        dup_edges = [e for e in graph.edges if e.kind == "DUPLICATES_WORKFLOW_PATTERN"]
        self.assertGreaterEqual(len(dup_edges), 1)

        # Generate prompt for quick_pay.py
        packer = SubgraphPacker(graph)
        prompt = packer.generate_llm_remediation_prompt("src/billing/quick_pay.py")

        self.assertIn("Behavioral Workflow Governance", prompt)
        self.assertIn("quick-pay-checkout", prompt)
        self.assertIn("submit-order-payment", prompt)
        self.assertIn("delegate", prompt)

    def test_allow_similar_workflows_suppresses_warning_and_graph_edge(self) -> None:
        """allow_similar_workflows acts as a declarative escape hatch for --strict."""
        write_code_and_synthetic(
            self.tmp_path,
            "app/checkout/flow_a.py",
            code_content="def checkout(): pass\n",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "flow-a",
                        "user_intent": "Checkout flow A",
                        "trigger": "ui.click:btn_a",
                        "steps": ["step:1", "step:2", "step:3", "step:4", "step:5"],
                        "outcome": "complete_a",
                    }
                ]
            },
        )

        # Flow B acknowledges flow-a in allow_similar_workflows
        write_code_and_synthetic(
            self.tmp_path,
            "app/checkout/flow_b.py",
            code_content="def checkout_b(): pass\n",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "flow-b",
                        "user_intent": "Checkout flow B",
                        "trigger": "ui.click:btn_b",
                        "steps": ["step:1", "step:2", "step:3", "step:4", "step:other"],
                        "outcome": "complete_b",
                        "allow_similar_workflows": ["flow-a"],
                    }
                ]
            },
        )

        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path, min_pattern_similarity=0.80)
        violations, _ = gov.check_repository()
        dup_violations = [v for v in violations if v.rule == "no-duplicate-workflows"]
        self.assertEqual(dup_violations, [])

        # Also verify no duplicate edge in graph
        graph = build_knowledge_graph(self.tmp_path, include_violations=False)
        dup_edges = [e for e in graph.edges if e.kind == "DUPLICATES_WORKFLOW_PATTERN"]
        self.assertEqual(dup_edges, [])

    def test_verify_synthetic_alignment_flags_missing_external_call(self) -> None:
        """Flags when .synthetic declares external:* but code makes no external calls/imports."""
        write_code_and_synthetic(
            self.tmp_path,
            "app/local_service.py",
            code_content="def compute(a, b):\n    return a + b\n",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "calculate-sum",
                        "user_intent": "Sum two numbers",
                        "trigger": "ui.click:btn",
                        "steps": ["validate:inputs", "external:remote_calc_service", "emit:result"],
                        "outcome": "calculated",
                    }
                ]
            },
        )

        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path, verify_alignment=True)
        violations, _ = gov.check_repository()
        align_violations = [v for v in violations if v.rule == "verify-synthetic-alignment"]
        self.assertEqual(len(align_violations), 1)
        self.assertIn("external:remote_calc_service", align_violations[0].message)
        self.assertIn("no network, I/O, or client call", align_violations[0].message)

    def test_verify_synthetic_alignment_flags_missing_emit(self) -> None:
        """Flags when .synthetic declares emit:* but code neither returns nor emits events."""
        write_code_and_synthetic(
            self.tmp_path,
            "app/silent.py",
            code_content="def process():\n    x = 10\n",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "silent-run",
                        "user_intent": "Run silent operation",
                        "trigger": "cron.tick",
                        "steps": ["validate:state", "mutate:state", "emit:audit_event"],
                        "outcome": "done",
                    }
                ]
            },
        )

        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path, verify_alignment=True)
        violations, _ = gov.check_repository()
        align_violations = [v for v in violations if v.rule == "verify-synthetic-alignment"]
        self.assertEqual(len(align_violations), 1)
        self.assertIn("emit:audit_event", align_violations[0].message)
        self.assertIn("no dispatch, emit, or event publication", align_violations[0].message)

    def test_verify_synthetic_alignment_flags_stub_orchestrator(self) -> None:
        """Flags when a file has zero symbols/calls yet claims an orchestrator workflow."""
        write_code_and_synthetic(
            self.tmp_path,
            "app/empty_stub.py",
            code_content="# TODO: implement\npass\n",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "stub-flow",
                        "user_intent": "Orchestrate complex flow",
                        "trigger": "ui.click",
                        "steps": ["validate:session", "mutate:state", "emit:notification"],
                        "outcome": "done",
                    }
                ]
            },
        )

        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path, verify_alignment=True)
        violations, _ = gov.check_repository()
        align_violations = [v for v in violations if v.rule == "verify-synthetic-alignment"]
        self.assertTrue(any("module contains no executable symbols" in v.message for v in align_violations))

    def test_verify_synthetic_alignment_passes_when_aligned(self) -> None:
        """Passes cleanly when declared action verbs match code reality."""
        code = (
            "import requests\n\n"
            "def checkout_item(item_id: str):\n"
            "    resp = requests.post('https://api.example.com/pay', json={'id': item_id})\n"
            "    events.emit('order_created', resp.json())\n"
            "    return resp.json()\n"
        )
        write_code_and_synthetic(
            self.tmp_path,
            "app/checkout_service.py",
            code_content=code,
            synthetic_payload={
                "workflows": [
                    {
                        "id": "checkout-order",
                        "user_intent": "Checkout order via API",
                        "trigger": "ui.click:pay",
                        "steps": ["validate:item", "external:payment_gateway", "emit:order_receipt"],
                        "outcome": "order_confirmed",
                    }
                ]
            },
        )

        gov = SyntheticWorkflowGovernance(root_dir=self.tmp_path, verify_alignment=True)
        violations, _ = gov.check_repository()
        align_violations = [v for v in violations if v.rule == "verify-synthetic-alignment"]
        self.assertEqual(align_violations, [])

    def test_verify_synthetic_alignment_rule_engine(self) -> None:
        """RuleEngine properly enforces verify-synthetic-alignment."""
        write_code_and_synthetic(
            self.tmp_path,
            "app/service.py",
            code_content="def compute(a):\n    return a\n",
            synthetic_payload={
                "workflows": [
                    {
                        "id": "service-flow",
                        "user_intent": "Process data",
                        "trigger": "api.call",
                        "steps": ["validate:input", "external:remote_gateway", "emit:done"],
                        "outcome": "done",
                    }
                ]
            },
        )
        manifest = {
            "version": 1,
            "rules": [
                {
                    "id": "verify-synthetic-alignment",
                    "type": "verify-synthetic-alignment",
                    "enabled": True,
                    "severity": "warning",
                }
            ],
        }
        violations = RuleEngine(manifest).scan(self.tmp_path, strict=True)
        self.assertTrue(any(v.rule_id == "verify-synthetic-alignment" for v in violations))


if __name__ == "__main__":
    unittest.main()
