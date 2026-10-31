# drift-be-gone knowledge graph walkthrough

The graph implementation lives in `drift/graph/`:

- `model.py` stores indexed nodes and edges and supports traversal, induced
  neighborhoods, JSON persistence, and Mermaid output.
- `extractors/` adds Python AST, JS/TS, UI bridge, workflow, and violation data.
- `solvers/` provides cycle, layer, blast-radius, abstraction, implementation
  shape, and prompt diagnostics.
- `builder.py` wires those pieces together; `cli.py` exposes `drift graph`.

## Find implementation-shape duplicates

The implementation-shape finder is intentionally an advisory signal. Python
functions are compared using normalized AST shape; JS/TS declarations use
normalized token shape. Local names and literal values are discarded, while
control flow, operators, and call patterns contribute to the similarity.
Candidates must live in different modules. A high score identifies code worth
comparing; it does not prove behavioral equivalence.

Try the synthetic example:

```bash
drift graph duplicates examples/intent-duplicates
drift graph duplicates examples/intent-duplicates --json
```

It contains two renamed implementations of a line-item total calculation and
an unrelated token policy function. The example manifest also enables the
`no-duplicate-implementations` warning rule:

```bash
drift check examples/intent-duplicates --strict
```

To enable the rule in another codebase, add an entry such as:

```json
{
  "id": "implementation-shape-review",
  "type": "no-duplicate-implementations",
  "enabled": true,
  "severity": "warning",
  "min_similarity": 0.86,
  "min_features": 8,
  "allow": ["tests/**", "**/*.test.*"]
}
```

The rule runs only when enabled. Keep it as a warning while tuning the
threshold and path exclusions for a repository; promote it to an error only
after reviewing its candidate quality.

## Graph commands

```bash
drift graph build . --output .drift/graph.json
drift graph explain . src/core/engine.py --mermaid
drift graph blast-radius . src/core/engine.py
drift graph duplicates . --min-similarity 0.86
drift graph prompt . src/core/engine.py
```
