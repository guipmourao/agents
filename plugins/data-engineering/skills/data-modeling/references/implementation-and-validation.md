# Implementation and Validation

## Physical implementation

Adapt recommendations to the target platform.

For BigQuery, prioritize:

- partitioning by business or ingestion date;
- clustering by frequently filtered or joined keys;
- `MERGE` or an equivalent idempotent strategy;
- controlled backfills;
- deterministic deduplication;
- separation between `raw`, `staging` or `core`, `audit`, and `mart`;
- safely reprocessable incremental models;
- scan-cost monitoring.

Do not recommend B-tree indexes, bitmap indexes, or physical constraints as universal solutions.

## Audit requirements

Include an audit dimension or audit fields containing at least:

- source;
- load execution identifier;
- ingestion timestamp;
- extracted period;
- schema version;
- validation status;
- technical hash or identifier when necessary.

## Security and privacy

Classify sensitive fields and recommend controls compatible with the environment:

- data minimization;
- masking;
- dataset or project segregation;
- row-level or column-level access policies;
- retention;
- traceability;
- consent and purpose limitation when applicable.

## Critical review

Before approval, verify:

- Is the grain explicit and consistent?
- Are different grains mixed?
- Can joins duplicate metrics?
- Are percentages being summed incorrectly?
- Are reach and other non-additive metrics handled correctly?
- Are conformed dimensions sufficient?
- Can natural keys collide across sources?
- Are there late-arriving facts or dimensions?
- Does the model support idempotent reprocessing?
- Is the physical design compatible with the selected platform?
- Are any fields missing a reliable definition?
- Does the model answer the declared business questions?

## Minimum validation queries or pseudocode

Provide checks for:

1. Duplicate rows at the declared grain.
2. Null business identifiers.
3. Orphaned fact-to-dimension references.
4. Temporal coverage gaps.
5. Source-to-target metric reconciliation.
6. Currency inconsistencies.
7. Additivity violations.
8. Metric multiplication caused by bridge tables.
9. Stable results after repeated processing.
10. Schema drift and unexpected field changes.
