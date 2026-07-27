# Dimensional Patterns

## Transaction fact table

Use a `Transaction Fact Table` when each row represents an individual, identifiable event.

Examples:

- click;
- impression;
- conversion;
- order;
- status change recorded as an event.

Require:

- event identifier or degenerate dimension when applicable;
- consistent timestamp;
- dimensions valid at the time of the event;
- metrics fully compatible with the event grain.

Do not force this pattern when the source already provides aggregated data.

## Periodic snapshot fact table

Use a `Periodic Snapshot Fact Table` when each row represents an entity state at regular intervals.

Examples:

- daily budget;
- daily balance;
- daily campaign status;
- end-of-day inventory;
- daily pacing.

Define:

- snapshot frequency;
- rule for missing periods;
- balance behavior;
- reprocessing policy;
- uniqueness key.

## Accumulating snapshot fact table

Use an `Accumulating Snapshot Fact Table` for processes with a predictable start, milestones, and completion.

Examples:

- approval lifecycle;
- order workflow;
- hiring process;
- project implementation;
- lead-to-conversion process.

Include role-playing date keys and support controlled updates as the process advances.

Do not use this pattern for continuous daily performance.

## Factless fact table

Use a `Factless Fact Table` when the analytical interest is occurrence, coverage, or eligibility.

Examples:

- entity active on a given day;
- presence or absence of activity;
- eligibility;
- association between entities;
- expected coverage without a recorded outcome.

Use a count column with value `1` only when documented and useful.

## Natural keys, surrogate keys, and degenerate dimensions

For each dimension:

- preserve the source natural key;
- create a source-independent surrogate key;
- include source identity in integration keys when collisions are possible;
- define members such as `unknown`, `not_applicable`, and `error`.

Use a `Degenerate Dimension` when an operational identifier has no descriptive attributes but is needed for traceability or drill-through.

## Role-playing dimensions

Use one physical dimension with distinct logical references when the same concept plays multiple roles.

Examples:

- creation date;
- start date;
- end date;
- click date;
- conversion date;
- processing date.

Name each foreign key according to its role.

## Junk dimensions, mini-dimensions, and outriggers

Use a `Junk Dimension` for stable combinations of low-cardinality flags and codes.

Do not use it for:

- free-form text;
- high-cardinality attributes;
- frequently changing fields;
- analytically irrelevant groupings.

Consider a `Mini-Dimension` when rapidly changing attributes would cause excessive Type 2 growth.

Use an `Outrigger` only when the benefit clearly outweighs the added complexity.

## Multivalued dimensions and bridge tables

Use a `Bridge Table` when an entity or fact relates to multiple members of the same dimension.

The bridge must declare:

- base entity key;
- multivalued dimension key;
- effective period when applicable;
- allocation factor when metrics could be duplicated;
- safe aggregation rule.

Never allow a many-to-many join to multiply financial or performance metrics without an explicit allocation rule.

## Slowly changing dimensions

For each mutable attribute, select:

- **Type 0:** preserve the original value;
- **Type 1:** overwrite without history;
- **Type 2:** create a new version with a new surrogate key;
- **Type 3:** preserve a previous value in an additional column;
- **hybrid:** combine behaviors only when required.

For Type 2, specify:

- `valid_from`;
- `valid_to`;
- `is_current`;
- surrogate key;
- change-detection rule;
- late-arriving fact handling;
- retroactive correction policy.

Do not apply Type 2 automatically to every attribute.
