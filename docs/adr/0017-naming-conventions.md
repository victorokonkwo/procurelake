# ADR 0017: Resolving naming conflicts between planning documents

- **Status:** Accepted
- **Date:** 2026-11-10

## Context
The implementation plan and the repository-structure plan use different names for the same things (open questions 23–26).

## Decision
| Topic | Chosen | Rejected |
|---|---|---|
| Cycle-time mart | `mart_procurement_cycle_time` (states what it measures) | `mart_procurement_cycle` |
| Bronze prefix for the OLTP source | `bronze/postgres/` | `bronze/postgresql/` |
| Terraform layout | `infrastructure/live/<env>/<area>/` + `infrastructure/modules/` | `infrastructure/terraform/environments/` |
| Python package root | `src/procurelake/` | `src/ingestion/` etc. |

In general, the repository-structure plan wins on layout, and the more descriptive name wins on model names. How DMS full-load and CDC output are laid out under `bronze/postgres/` versus `bronze/cdc/` will be settled in ADR 0014 (Week 5).

## Consequences
All docs and code use the names above.
