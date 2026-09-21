# Planning Principles

## Behaviour over implementation

The unit of planning is a behavioural capability.

Do not define features merely because a change occurs in a particular file or architectural layer.

A single feature may require changes across frontend, backend, persistence, tests, integrations, and configuration.

## ATDD orientation

Acceptance criteria describe observable outcomes.

Prefer:

Given a project has no patient list
When approval is submitted
Then the user is warned that the export is unrestricted

over:

Add a warning component to ApprovalPage.

Use Given / When / Then where it makes behaviour clearer.

Acceptance criteria should cover normal behaviour and important alternative or failure paths.

## User decisions versus agent decisions

Ask the user when a decision affects:

- desired product behaviour;
- user experience;
- security or privacy;
- externally visible compatibility;
- significant architectural direction;
- scope;
- important data semantics.

Resolve the decision yourself when repository conventions provide a clear answer or the choice is an implementation detail with no meaningful behavioural consequence.

When there are meaningful alternatives, present them concisely with pros and cons.

## Existing patterns

Before proposing something new, look for existing abstractions, workflows, validation patterns, error handling, UI conventions, persistence patterns, integration patterns, and testing patterns.

Do not reuse an abstraction blindly. Verify that its semantics fit the new behaviour.

## Feature independence

Treat each feature specification as the contract between the planning agent and an implementation agent.

An implementation agent should be able to understand:

- what the feature does;
- why it exists;
- what it depends on;
- relevant existing code;
- expected behaviour;
- acceptance criteria;
- important failure cases;
- testing expectations.

The implementation agent should not need the planning conversation.

## Relevant code

Distinguish existing relevant code from likely changes.

Do not claim an exact file must change unless the evidence supports that conclusion.

Use language such as "likely changes" or "implementation agent should verify" when appropriate.

## Edge cases

Prioritise edge cases involving:

- ambiguous user intent;
- data integrity;
- unintended data exposure;
- permissions;
- external systems;
- important state distinctions;
- existing invariants;
- likely implementation mistakes.

Do not attempt to catalogue every theoretical failure.

## Open issues

Distinguish:

- open question — requires a decision;
- assumption — unconfirmed interpretation used to continue;
- limitation — known constraint;
- deferred decision — deliberately postponed;
- out of scope — intentionally excluded.

Do not hide unresolved issues inside implementation notes.

## Exploration stopping rule

Stop repository exploration when you can confidently explain:

1. What currently happens.
2. What needs to change.
3. Where the relevant behaviour lives.
4. What existing mechanisms can be reused.
5. What observable behaviours are required.
6. What important consequences the change has.
7. What information remains unknown.

If one cannot be answered, investigate further or ask the user.
