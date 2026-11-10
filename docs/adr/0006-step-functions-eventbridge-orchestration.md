# ADR 0006: Step Functions + EventBridge orchestration

- **Status:** Accepted
- **Date:** 2026-11-10
- **Source:** ProcureLake implementation plan, architectural decisions table

## Context
Pipelines need scheduling, retries, and visible state tracking.

## Decision
AWS Step Functions state machines (ASL in `orchestration/step_functions/`) orchestrate the pipelines, and EventBridge triggers them on a schedule (`orchestration/eventbridge/schedules.yaml`).

## Consequences
- Execution history doubles as an operational record.
- Nothing always-on is needed.
- Where dbt runs is still open (open question 16).

## Alternatives considered
None recorded in the plan.
