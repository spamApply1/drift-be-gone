# drift-be-gone (drift)

**Stop your architecture from rotting while an agent writes half the code.**

`drift-be-gone` is a manifest-driven architecture gate **and** repo-understanding
map for agentic codebases: repos where code generation is fast enough that the
dependency graph quietly turns into spaghetti. It is pure Python, local-first,
and has no build step — point it at a repo and it tells you both *what exists*
and *what just broke the rules*.

It does two jobs from one dependency graph:

- **`drift check`** — enforce architectural intent (no cycles, layering,
  forbidden dependencies, orphan modules, missing entrypoints).
- **`drift map`** — emit a JSON understanding artifact (repo map, dependency
  graph, notable files, inferred domains, symbol index) that a coding agent or
  an LLM harness can read to know what to touch before it edits anything.

## ⚡ Quickstart

```bash
python3 -m pip install -e .

# enforce architecture
drift check . --json --strict

# emit the understanding map an agent can consume
drift map . --output .drift/map.json
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

`no-cycles` and `orphan-modules` ship on by default. `layering`,
`forbidden-deps`, and `entrypoints-exist` are opt-in — declare your intent in
the manifest and they enforce it.

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
