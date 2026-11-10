# ADR 0007: DynamoDB for pipeline checkpoints and run metadata

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
CDC apply must be idempotent and recoverable. It needs a durable record of which input manifests have been processed and the last good checkpoint.

## Decision
DynamoDB stores completed input manifests, checkpoints, and run metadata. A checkpoint advances **only after** the Iceberg write and its validation succeed. Overlapping writers on the same target table are prevented; the locking mechanism is open question 15.

## Consequences
- Safe replay after a failure.
- Exactly-once results are achieved by idempotent merge plus checkpointing, not by the transport.

## Alternatives considered
None recorded in the plan.
