# Incident report: <short title>

| | |
|---|---|
| Date | YYYY-MM-DD |
| Environment | dev |
| Severity | <SEV-1 data loss / SEV-2 SLO missed / SEV-3 degraded, no SLO impact> |
| Status | <resolved / mitigated / open> |
| Drill or real | <drill (controlled) / real> |

## Summary
One paragraph: what failed, who or what was affected, and how it ended.

## Timeline (UTC)
| Time | Event | Evidence |
|---|---|---|
| | Failure starts | |
| | First alert (alarm or event) | alarm name, SNS message |
| | Diagnosis | |
| | Recovery action | |
| | Service restored (data correct in Gold) | |
| | Alarm OK | |

## Impact
- Stale window (oldest affected change committed → visible in Gold):
- Changes affected (by table):
- Lost or duplicated changes (measured):
- SLO impact (CDC freshness: 95% within 15 min):

## Detection
How it was detected, how long that took, and whether the pipeline reported it itself.

## Root cause

## Recovery
Steps taken (link the runbook) and how correctness was checked.

## What went well / what did not

## Actions
| Action | Owner | Status |
|---|---|---|

## Evidence
Raw records (`build/evidence/…`), execution names, query IDs.
