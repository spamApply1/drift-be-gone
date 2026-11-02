# drift-be-gone (drift)

**Stop your architecture from rotting while an agent writes half the code.**

`drift-be-gone` is a manifest-driven architecture gate **and** repo-understanding
map for agentic codebases: repos where code generation is fast enough that the
dependency graph quietly turns into spaghetti. It is pure Python, local-first,
and has no build step — point it at a repo and it tells you both *what exists*
and *what just broke the rules*.

It does three jobs from one dependency graph:

- **`drift check`** — enforce architectural intent (no cycles, layering,
  forbidden dependencies, orphan modules, missing entrypoints).
- **`drift map`** — emit a JSON understanding artifact (repo map, dependency
  graph, notable files, inferred domains, symbol index) that a coding agent or
  an LLM harness can read to know what to touch before it edits anything.
- **`drift graph`** — construct a unified multi-domain knowledge graph across code AST,
  UI bindings, and workflow gates; run graph-theoretic cycle cutting and layer tracing;
  and synthesize zero-fluff LLM remediation prompts to resolve architectural debt.

## ⚡ Quickstart

```bash
python3 -m pip install -e .

# enforce architecture (pass --graph for intelligent remediation advice)
drift check . --json --strict --graph

# emit the understanding map an agent can consume
drift map . --output .drift/map.json

# build the semantic knowledge graph
drift graph build . --output .drift/graph.json

# calculate change impact & blast radius for a module
drift graph blast-radius . path/to/module.py

# surface renamed functions with similar implementation shapes
drift graph duplicates . --min-similarity 0.86

# generate a structured LLM remediation prompt for an architectural issue
drift graph prompt . path/to/violating_file.py
```

Clean output is the sound of an architecture staying honest:

```text
No architecture violations found.
```

## 🧱 What it enforces

| Rule | What fails |
| --- | --- |
| `no-cycles` | circular import chains between modules |
| `layering` | a lower layer importing a higher one (declared layer order) |
| `forbidden-deps` | any import matching a forbidden `from` → `to` path pair |
| `orphan-modules` | modules nothing imports (dead code / missing wiring) |
| `entrypoints-exist` | declared entrypoints that are missing on disk |
| `canonical-abstractions` | direct use of restricted libraries, primitives, or symbols outside declared canonical modules (forces reuse/abstraction) |
| `resolve-imports` | a symbol *called* in a module that is neither imported nor declared there, yet is exported by another module in the repo (catches "used but never imported" ReferenceErrors) |
| `no-duplicate-symbols` | duplicate top-level class or function names across distinct modules (catches copy-pasting and wheel rebuilding) |
| `no-duplicate-implementations` | similar normalized function shapes across modules, even when names differ (review candidates, not proof of equivalent behavior) |
| `require-synthetic` | non-exempt code files lacking a companion `.synthetic` file defining user workflows |
| `no-duplicate-workflows` | duplicate or near-duplicate user-journey workflow step sequences across different modules |
| `verify-synthetic-alignment` | `.synthetic` declared steps that do not match the AST/token reality in code (prevents synthetic drift) |

`no-cycles` and `orphan-modules` ship on by default. `layering`,
`forbidden-deps`, `entrypoints-exist`, `canonical-abstractions`, and
`no-duplicate-symbols`, and `no-duplicate-implementations` are opt-in — declare
your intent in the manifest and they enforce it.

`no-duplicate-implementations` is also opt-in and defaults to warning severity.
It compares Python AST shapes and JS/TS token shapes while normalizing local
names and literal values. Similar control flow, operators, and call patterns
produce candidates for review; a structural match alone cannot establish that
two functions have the same behavior. Exclude intentional matches with its
`allow` path patterns. The standalone `drift graph duplicates` command prints
the same candidates without changing the check result.

### 🛡️ Forcing Reusable Abstractions (Anti-Rebuilding The Wheel)

When AI agents or fast-moving teams write code, they often re-implement
capabilities (e.g. WebRTC, chat sockets, auth headers) in multiple feature
folders instead of abstracting into a shared pattern.

`canonical-abstractions` gates this behavior by confining low-level primitives
and third-party libraries to their blessed canonical path:

```json
{
  "id": "canonical-webrtc",
  "type": "canonical-abstractions",
  "enabled": true,
  "severity": "error",
  "canonical": "src/shared/rtc/**",
  "restricted_imports": ["simple-peer", "peerjs", "aiortc"],
  "restricted_primitives": ["RTCPeerConnection", "mediaDevices.getUserMedia", "RTCDataChannel"],
  "allow": ["tests/**", "**/*.test.*"],
  "message": "Direct WebRTC usage is restricted. Abstract logic into src/shared/rtc and reuse it."
}
```

If an agent attempts to instantiate `RTCPeerConnection` or import `simple-peer`
in `src/features/chat.ts`, `drift check` fails immediately at the gate:

```text
src/features/chat.ts:14: [canonical-webrtc] Direct WebRTC usage is restricted. Abstract logic into src/shared/rtc and reuse it.
```

Moreover, `drift map` automatically includes declared canonical abstractions so
coding agents inspecting `.drift/map.json` know what reusable abstractions exist
before writing any new code.

### 🔗 Resolving Missing Imports (`resolve-imports`)

Fast code generation produces a specific, nasty bug: a module *calls* a helper
that lives in a sibling module but the `import` line never gets written. The
dependency graph still looks fine — there is simply no edge where one should be
— so pure import-graph analysis stays silent while the code throws
`ReferenceError: hasUserMedia is not defined` the moment that path runs.

`resolve-imports` closes that gap. For every JS/TS module it collects the
symbols that are *called* (`foo(...)`, `new Foo(...)`) and checks each one
resolves to either a local declaration (import, function, class, `const`/`let`/
`var`, or parameter) or a known runtime global. A call that resolves to none of
those — **but is exported by another module in the repo** — is reported:

```json
{
  "id": "resolve-imports",
  "type": "resolve-imports",
  "enabled": true,
  "severity": "error",
  "allow": ["tests/**", "**/*.test.*", "**/vendor/**"],
  "globals": ["gtag", "dataLayer"]
}
```

```text
public/js/templates/shared.js:1462: [resolve-imports] 'hasUserMedia' is called but never imported or defined in this module. It is exported by 'public/js/camera-capture.js' — add the missing import.
```

The cross-module export requirement keeps the signal tight: the symbol provably
exists somewhere in the codebase, yet is unresolved where it is used, so the
report is almost always a genuinely missing import rather than a typo or an
intentional global. Add extra ambient globals (analytics shims, injected SDKs)
via `globals`, and exclude files with `allow`.

### 🎭 Behavioral & User-Journey Governance (`.synthetic` Companion Files)

Enforcing `.synthetic` companion files shifts `drift-be-gone` from purely structural AST/import governance to **behavioral and user-journey governance** (`require-synthetic` + `no-duplicate-workflows`). Instead of only tracking which module imports which library, the engine and knowledge graph index what the user is actually doing (triggers, intent sequences, state mutations, and outcomes) and flag when two parts of the codebase quietly build parallel flows for the same user capability.

Each source file `foo.py` (or `foo.ts`) can have a sibling `foo.py.synthetic` (or `foo.synthetic`) file:

```json
{
  "file": "src/checkout/payment.py",
  "role": "orchestrator",
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
        "emit:notification_receipt"
      ],
      "outcome": "order_paid_and_confirmed"
    }
  ]
}
```

- **Action-Verb Taxonomy**: Steps are normalized into `<action_type>:<domain_concept>` tokens (`validate:*`, `mutate:*`, `external:*`, `emit:*`, `verify:*`).
- **`require-synthetic`**: Ensures non-exempt files declare user workflows.
- **`no-duplicate-workflows`**: Multi-file participation in the same workflow ID is valid, but two different workflow IDs building duplicate or near-duplicate user interaction chains are flagged.
- **Declarative Escape Hatch (`allow_similar_workflows`)**: If two workflows legitimately share sequences, add `"allow_similar_workflows": ["<other-id>"]` to the workflow definition to suppress near-duplicate warnings in `--strict` mode and remove duplicate graph edges.
- **`verify-synthetic-alignment`**: Cross-checks `.synthetic` declarations against the code AST/token reality to prevent synthetic drift (e.g., verifying `external:*` calls external I/O or client libraries, `emit:*` dispatches events, and stub files cannot claim orchestrators).
- **`drift map` Integration**: Emits `canonical_workflows` with user intents, triggers, outcomes, steps, and participating files.
- **`drift graph prompt` Integration**: Automatically tells coding agents when a target module implements a flow that duplicates an existing workflow and guides them to delegate.

## 🧠 Knowledge Graph & LLM Architectural Resolution

When coding agents face complex architectural debt (e.g. circular dependencies or layer inversions), flat lists of error messages force the agent to spend tens of thousands of tokens guessing how to refactor.

`drift graph` transforms the repository into a unified **Semantic Property Graph**:

- **Multi-Domain Extraction**: Combines Python AST definitions & calls, JS/TS imports & classes, UI `data-action` dispatches, CI workflow gates, and rule violations into one connected ontology.
- **Cycle Cutter (Feedback Arc Set)**: Projects module cycles down to individual symbol-level calls and calculates minimum-cut seam recommendations (`extract_to_shared`, `dependency_injection`, `dynamic_import`).
- **Layer Boundary Path Tracer**: Finds direct and transitive breach paths across architectural tiers and suggests interface inversion points.
- **Blast-Radius & Instability Analysis**: Computes afferent (incoming callers) and efferent (outgoing dependencies) coupling, estimating risk levels before refactoring.
- **LLM Subgraph Packer**: Emits compact, token-optimized context blocks containing Mermaid flowcharts, minimal-cut candidates, and invariant constraints ready for agent prompt injection.
- **Implementation Shape Finder**: Finds cross-module functions with similar normalized AST/token structure and includes nearby matches in coding-agent remediation prompts.

```bash
# Explain a module's neighborhood and risk level
drift graph explain . src/core/engine.py --mermaid

# Generate an end-to-end prompt for an LLM to resolve a circular dependency
drift graph prompt . src/core/engine.py

# inspect likely copy-paste candidates by function shape
drift graph duplicates . --json
```

## 🧠 How it works

- Builds a module dependency graph from **Python** (`ast`-based imports) and
  **JS/TS** (import/require) with zero external dependencies.
- `check` runs the graph through your manifest and exits non-zero on violations;
  `--json` for automation, `--strict` to make warnings blocking.
- `map` turns the same graph into an understanding artifact: `graph_nodes`,
  `graph_edges`, `notable_files` (ranked by in-degree), `inferred_domains`, and
  a `symbol_index` of functions/classes per file.

## 🧬 Why it replaces a heavyweight "understanding" tool

Agentic harnesses need to know *what exists and what to touch*. Instead of a
separate binary that has to be built and maintained, `drift map` produces that
context from the same graph it polices — so the map an agent reads and the rules
that gate the agent's output can never disagree.

## 🌌 Part of the be-gone ecosystem

One repo per concern, same `check --manifest --json --strict` contract:

- [slop-be-gone](https://github.com/spamApply1/slop-be-gone) — code hygiene
- [design-be-gone](https://github.com/spamApply1/design-be-gone) — design consistency
- [chaos-be-gone](https://github.com/spamApply1/chaos-be-gone) — workflow sanity
- **drift-be-gone** — architecture + understanding (you are here)

The thesis: push enforcement outward — hygiene, design, workflow, architecture —
until there is essentially one canonical way for anything to exist.

## ⭐ Try it

Run `drift check .` on your repo, star it if it caught something, and compose a
manifest that encodes *your* architecture. Standards should be executable.

## Tests

```bash
PYTHONPATH="$PWD" python3 -m unittest discover -s tests -t .
```
