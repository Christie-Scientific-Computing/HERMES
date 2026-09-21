---
name: implementation-cycle
description: "Defines the full workflow for implementing a single feature from an existing overarching plan file: implement against the plan's spec with targeted tests for core logic, then run an independent review/convergence loop before opening a PR. Use this whenever the user says \"start implementation cycle,\" \"implement this feature,\" \"start the implementation loop,\" or points to a plan file plus a specific feature to build. Requires both a plan file path and a named feature to begin — do not start coding without them. Make sure to trigger this any time the user is asking Claude Code to build out a feature from a broader plan/roadmap document, even if they don't use the exact phrase \"implementation cycle.\""
---

# Implementation Cycle

A disciplined workflow for implementing one feature at a time: read the plan → scope it → write a short plan of action → branch → implement against spec with targeted tests → get it independently reviewed → converge → refresh the architecture map → open a PR → report back.

## Hard requirement to start

This skill needs two things before any code is written:
1. **A path to an overarching plan file** (e.g. `PLAN.md`, or `.plans/<feature-name>/plan.md`) — written by another agent/process, containing a list of features. This may be an index linking out to per-feature spec files rather than containing full detail inline.
2. **A specific feature** from that file to implement in this cycle.

If either is missing, ask for it and stop. Do not guess at which feature to implement or proceed on partial information.

## Step 1 — Read the plan and scope the feature

Read the plan file and locate the named feature. The plan file is often an index: each feature entry may link to a separate feature spec file (e.g. `features/F001-<name>.md`, as produced by the `feature-planner` skill) rather than containing the full spec inline. If the entry links to a feature file, read that file too — it holds the acceptance criteria, edge cases, and other detail needed to scope and implement the feature.

**Orient in the codebase via `ARCHITECTURE.md` first, before any of your own exploration.** If a repo-root `ARCHITECTURE.md` exists, read it and check its freshness the same way the `architecture-map` skill does: run that skill's `scripts/staleness_key.sh` against the repo root and compare the result to the `staleness_key` recorded in the file's frontmatter.
- **Fresh, and it answers what you need** (which app/module owns the area this feature touches, existing conventions to follow, related domains, where similar prior features live) → use it as the answer and skip redundant exploration of that ground.
- **Fresh, but it doesn't cover something you need** (e.g. a specific function signature, a test convention at a level of detail the map doesn't capture) → fall back to your own exploration (`Read`/`grep`/an `Explore` agent) for just that gap, not the whole codebase.
- **Stale, or `ARCHITECTURE.md` doesn't exist** → fall back to your own exploration as normal. Don't block on regenerating the map here — that's Step 7's job, after the feature is implemented.

This is meant to save redundant re-exploration of ground the map already covers, not to replace judgment — treat it as a structural orientation aid (per its own header), and verify anything load-bearing (exact APIs you're about to call, exact file paths you're about to edit) against the actual code before relying on it.

Check whether the feature is actually implementable as scoped: does it have clear boundaries and enough detail to know what "done" looks like (acceptance criteria, expected behavior, what's explicitly out of scope)?

- **If the feature is clearly enough described** (even if some small implementation details are left to judgment) → proceed to Step 2.
- **If it's genuinely too vague or missing key scope-defining details** (e.g. no clear acceptance criteria, ambiguous boundaries, conflicting requirements) → stop here. Tell the user exactly what's missing or unclear, and what you need added to the plan file before you can proceed. Don't guess on anything that defines the scope of the feature.

Minor implementation-detail ambiguity (e.g. "should this be a dataclass or a dict") is fine to resolve with judgment later — that's not a reason to stop.

## Step 2 — Write a brief plan of action

Before writing any code, write a short plan of action directly in your response (not a file, no approval gate needed — state it and move straight into Step 3). Cover:

- **Approach summary** — how you're going to implement this
- **Files to touch** — which files you expect to create/modify
- **Security risks** — anything worth flagging given what this touches (input validation, auth boundaries, data exposure, injection risks, dependency risks, etc.). If genuinely none apply, say so briefly rather than omitting the section.
- **Open questions** — anything you're resolving via judgment call, stated explicitly so it's visible later

This is a "show your work" step, not a checkpoint — proceed immediately after writing it.

## Step 3 — Check out a new branch

Before writing any tests or code, check out a new git branch for this feature (branched off the current base branch). Name it something clearly tied to the feature. Do this once, before starting implementation — not again on each review round.

## Step 4 — Implement against spec, with targeted tests

Implement the feature to meet the spec defined in the plan file (the "why" and "what" of the feature) and the plan of action from Step 2. This is spec-driven, not strict TDD — don't write a test for every code path before writing any implementation, and don't aim for exhaustive coverage. That bloats the context window and slows things down for little benefit.

Instead:
- Write tests for the **core logic** — the parts of the feature that carry real risk of being wrong (business logic, non-trivial branching, anything flagged as a security risk in Step 2). Write these alongside or just ahead of the corresponding implementation, not all tests up front.
- Skip tests for trivial glue code, straightforward wiring, or thin wrappers around well-tested libraries/frameworks — these aren't worth the context spent testing them.
- Match whatever testing framework and conventions the repo already uses; default to `pytest` if there's no existing convention.
- Run the test suite before moving on and confirm everything passes.

## Step 5 — Independent review

Once tests pass, spawn an independent subagent (via the Task tool) to review the work. This subagent should have **fresh context** — no memory of having written the code — and should be given:
- The diff / changed files
- The plan of action from Step 2
- The feature description (from the plan file, and its linked feature file if there is one)

Ask it to review for, explicitly:
- **Clarity** — is the code readable and reasonably self-explanatory?
- **Style** — does it match the repo's existing conventions?
- **Security vulnerabilities** — think adversarially about edge cases and misuse
- **Functionality** — does it actually do what the feature requires, including edge cases?
- **Performance** — any obvious inefficiencies or scaling concerns?

## Step 6 — Converge

- If the reviewer raises issues: address them, return to Step 4 (implementation) to fix them, adding/adjusting targeted tests as needed, then re-run Step 5 (review) with a fresh subagent.
- This can repeat for **up to ~3 rounds total**.
- If it hasn't converged after 3 rounds, stop looping. Surface the remaining review issues directly to the user rather than continuing silently — let them decide how to proceed.

## Step 7 — Refresh the architecture map

Once converged (or capped at 3 rounds and the user has weighed in on how to proceed), invoke the `architecture-map` skill on this repository before opening the PR. This feature's changes are exactly the kind of thing that makes a previously-generated map stale, and the map is meant to stay current for whichever skill or session next needs orientation — don't let this cycle be the reason it drifts.

- If the skill reports the map was already fresh, there's nothing to commit — move on to Step 8.
- Otherwise, it will have written or updated `ARCHITECTURE.md` (and possibly `.architecture/domains/*.md`). Commit that as its own commit on this feature branch — a separate commit from the feature's implementation commit(s), so the history distinguishes "the feature" from "the doc update the feature caused" — before pushing.

## Step 8 — Open a pull request

Push the branch (including the architecture-map commit from Step 7, if any) and open a pull request. The PR description must include:

- **What was the aim/feature and any considerations** — pull from the plan file entry and Step 2's plan of action
- **How did it do it?** — implementation approach, and any design decisions made along the way
- **Any issues the review process highlighted and how they were addressed** — summarize each review round from Step 6
- **Known limitations or things I should know about** — anything left unresolved, deliberately out of scope, or worth a human's attention

## Step 9 — Wrap up

Report back to the user in chat with:
- A link/reference to the PR opened in Step 8
- Test results
- Whether the architecture map was refreshed (Step 7) or was already fresh
- Any assumptions or judgment calls made along the way (Step 1 open questions, Step 2 open questions, and anything else that came up)
- **If the feature turned out bigger than the plan implied**: use best judgment to keep going rather than stopping, but state clearly here what grew beyond the original scope and why you made the calls you did — don't bury this.
