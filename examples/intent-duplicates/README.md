# Synthetic implementation-shape example

This small repository has two differently named implementations of the same
line-item total pattern. The `security` function is intentionally unrelated.

From the `drift-be-gone` checkout, inspect candidates with:

```bash
drift graph duplicates examples/intent-duplicates
```

The opt-in manifest in this directory also enables the candidate rule for
`drift check`. A match is a review prompt based on structure, not proof that
the functions are behaviorally interchangeable.
