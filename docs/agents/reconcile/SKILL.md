---
name: reconcile
description: "Re-derive the global contract after a wave of work: replay the deltas from closed tickets and changed normative sentences (spec/ADR/CONTEXT/AC), detect composition drift, and write corrections back to the normative layer. Use before starting a ticket whose blockers just closed, after a wave of ticket closures, whenever a diff adds or changes a normative sentence, and before an irreversible production step."
---

# Reconcile

**Composition drift** is the failure mode: every ticket satisfies its own AC, yet the union no longer satisfies the spec. A precondition disappears, a term gains a second meaning, a rule lives only in an ADR, a dependent ticket inherits a stale assumption. Recording every change is necessary but not sufficient — the global model is a *derived* artifact, and no ticket owns it.

This skill replays only what changed since the last **checkpoint**, so the pass stays cheap enough to run often.

## When to run (timing)

| Trigger | Scope |
| --- | --- |
| A ticket's blockers just closed, before the dependent ticket starts | predecessor union |
| A diff adds or changes a normative sentence (spec / ADR / CONTEXT / AC) | that invariant and its consumers |
| A wave of tickets closes (spec section done, milestone, release) | full |
| Before an irreversible operation (production cutover, migration, backfill) | full, plus gate evidence |
| N closures or N days since the checkpoint (default 10 closures / 7 days) | full |

If no checkpoint exists, bootstrap from the spec and the current ticket set before proceeding.
Do not defer to the end: the longer the delta, the more expensive the reconstruction and the more likely a defect has already been built on.

## The checkpoint

Keep `docs/reconcile/CHECKPOINT.md` (or the path named in `AGENTS.md`):

- checkpoint commit + date;
- spec issue and its last revision timestamp;
- ticket set with status (closed / open / dropped);
- **invariants table**, one row each: id, statement, source of truth (file + line or issue section), consumers;
- open defects: id, class, owner, blocking?;
- next trigger.

The checkpoint is the baseline. The pass is the issue and `git diff` delta since it.

## Steps

1. **Load the checkpoint.** If absent, build the initial invariants table from the spec, ADRs, CONTEXT and the open/closed tickets, and say so in the report.
2. **Collect deltas.** Facts only: closed/edited tickets and closing comments, spec/ADR/CONTEXT/AGENTS diffs, code diffs that touch invariant surfaces. No opinions yet.
3. **Rebuild the union.** For each invariant, derive the current statement from the union of deltas — never from the newest ticket. Record provenance per sentence.
4. **Detect composition defects**, one row each:
   - **Contradiction** — two sources state different behaviour.
   - **Orphan rule** — a normative sentence exists only in an ADR (rationale layer) and never in spec/AC.
   - **Missing precondition** — a dependent ticket body omits a precondition its predecessor established.
   - **Term collision** — one word, two live meanings.
   - **Stale pointer** — AGENTS/knowledge-graph points at a replaced rule or file.
   - **Implementation divergence** — code contradicts a normative sentence added in the delta.
   - **Unowned decision** — a question was answered in prose but has no ticket, ADR or spec home.
5. **Route before fixing.** Implementation divergence → code fix. Contract defect → spec/AC amendment. Term → CONTEXT. Navigation → pointer fix. Unowned decision → decision ticket. Never fix code while the normative layer is still contradictory.
6. **Write back in order:** CONTEXT → spec/AC → ADR (rationale only) → AGENTS/knowledge-graph pointers → dependent ticket bodies → code.
7. **Verify** — all must hold:
   - every invariant has exactly one source of truth;
   - every dependent ticket body carries its preconditions;
   - no term has two live meanings;
   - every defect has an owner or is closed;
   - a fresh reader of the docs answers a dry-run question about the changed invariant correctly.
8. **Update the checkpoint** with the new commit, date, invariants table, defect ledger and next trigger. Write the report to `docs/reconcile/reports/<yyyy-mm-dd>.md`.

## What counts as an invariant

Start from the project's own list; typical classes:

- **ownership** — who may write/read each fact and derived table;
- **preconditions** — what must exist before a path may run, and what it does when unmet;
- **units and keys** — amounts, volumes, business keys;
- **ordering** — fallback chains, pipeline stages;
- **state machines** — coverage, pending, lease;
- **terms** — the glossary's canonical words.

## Completion criteria

A reconcile is done when the invariants table matches the current docs, every delta since the checkpoint has been replayed, no defect is unowned, and the checkpoint is committed. "Docs read" is not done.

## Anti-patterns

- Reconciling only docs or only code.
- Treating a closed ticket as proof of the global contract.
- Using the latest ticket as the model.
- Rewriting the spec wholesale instead of patching the drifted invariants.
- Deferring to the end to save time — the end is where the expensive defects land.
