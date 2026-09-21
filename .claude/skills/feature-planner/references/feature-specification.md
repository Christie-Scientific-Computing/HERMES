# Feature Specification

Each feature must describe a behavioural capability and be sufficiently self-contained for an independent implementation agent.

## Required structure

### Purpose

Explain why the feature exists and what capability it provides.

### Behaviour

Describe intended observable behaviour without turning this into a line-by-line implementation recipe.

### Dependencies

List prerequisite feature IDs and explain each dependency. Use `None` when independent.

### Relevant code

Separate:

- Existing — code establishing current behaviour or providing reusable functionality.
- Likely changes — code likely to be affected.

Include paths and concise reasons.

### Acceptance criteria

Use concrete behavioural scenarios.

Each scenario should establish preconditions, action, and observable result.

Prefer Given / When / Then.

Cover important normal, alternative, validation, permission, persistence, integration, and failure behaviours relevant to the feature.

### Edge cases

Include meaningful cases requiring implementation attention or a behavioural decision.

### Success

State what demonstrates completion and keep it consistent with the acceptance criteria.

### Failure behaviour

Describe expected behaviour when relevant operations fail.

### Testing considerations

Describe how existing repository testing conventions apply.

Mention relevant test levels, fixtures, factories, helpers, analogous tests, or integration boundaries.

### Implementation notes

Record architectural context, contracts, constraints, and decisions that an implementation agent needs.

Avoid unnecessarily prescribing exact implementation.

### Out of scope

Explicitly exclude related behaviour that might otherwise be inferred as part of this feature.

## Feature naming

Use short behavioural names.

Good:

- Select an export destination
- Persist approved export configuration
- Warn about unrestricted exports

Avoid:

- Add DestinationSelector component
- Add database column
- Update API endpoint

## Acceptance criteria quality

Weak:

> An ExportConfig object is created.

Better:

> Given an approved project, when the project is reopened, then its configured export destination is displayed.

The second describes externally observable behaviour rather than an implementation detail.

## Independence check

Before finalising a feature specification, ask:

> Could an implementation agent receive only this specification and the repository and implement and test the feature without needing to ask what the planning conversation meant?

If not, improve the specification, clarify the requirement, or add an explicit dependency.
