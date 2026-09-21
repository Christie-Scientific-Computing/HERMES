# Feature F005: Mandatory project description

## Purpose

Project descriptions are optional today, which produces ethics-workflow
records with no context for a reviewer. This feature makes the field
required going forward, without touching existing records.

## Behaviour

Project creation fails validation if the description field is left blank.
Existing projects with a blank description are unaffected — they are not
retroactively invalidated or blocked from any further workflow action.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/forms/research_projects.py` (~line 17) —
  `description = TextAreaField("Description", validators=[OptionalField()])`.

### Likely changes

- `frontend_fastapi/forms/research_projects.py` — swap `OptionalField()` for
  `DataRequired()` (or add it alongside any existing length validators).

## Acceptance criteria

### Scenario: Creation blocked without a description

**Given** a user filling out the project creation form

**When** they submit with the description field empty

**Then** the form re-renders with a field-level "this field is required"
error and no project is created

### Scenario: Creation succeeds with a description

**Given** a user filling out the project creation form

**When** they submit with a non-empty description

**Then** the project is created as today, unaffected by this change

### Scenario: Existing blank-description projects are unaffected

**Given** a project created before this change, with a blank description

**When** it is viewed, submitted for review, approved, or otherwise
progressed through its existing workflow

**Then** nothing about that workflow is blocked or altered by the now-blank
description — no backfill, no forced edit

## Edge cases

- Whitespace-only description (e.g. a single space) — decide whether
  `DataRequired()`'s default stripping behaviour is sufficient, or whether
  an explicit `.strip()` check is needed; WTForms' `DataRequired` does
  reject whitespace-only input by default for `TextAreaField`, so no extra
  code should be needed — verify against the actual WTForms version pinned
  in this repo.

## Success

Attempting to create a project with an empty description is rejected at the
form level; an existing project with a blank description continues to work
through every existing view/route without modification.

## Failure behaviour

Standard WTForms field-level validation error, consistent with every other
required field on this form.

## Testing considerations

- `frontend_fastapi/tests/test_research_projects.py` — add a case asserting
  creation fails with an empty description, alongside whatever existing
  cases cover other required fields on this form.

## Implementation notes

This is the smallest feature in the round — a one-line validator change. No
backend/schema change: `research_projects.description` already exists as a
column and already accepts (and will continue to accept, for old rows) a
blank value at the database level; only the frontend form validation
changes.

## Out of scope

- Any migration or backfill of existing blank descriptions.
- Any change to how description is displayed or stored.
