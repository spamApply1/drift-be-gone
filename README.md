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
| `canonical-abstractions` | direct use of restricted libraries, primitives, or symbols outside declared canonical modules (forces reuse/abstraction) |
| `no-duplicate-symbols` | duplicate top-level class or function names across distinct modules (catches copy-pasting and wheel rebuilding) |

`no-cycles` and `orphan-modules` ship on by default. `layering`,
`forbidden-deps`, `entrypoints-exist`, `canonical-abstractions`, and
`no-duplicate-symbols` are opt-in — declare your intent in the manifest and they
enforce it.

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
