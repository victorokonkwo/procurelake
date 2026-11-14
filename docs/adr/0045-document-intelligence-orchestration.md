# ADR 0045: Document intelligence state machine, started when a document lands

- **Status:** Accepted (owner decision 2026-11-14; resolves open question 41). Built and tested
  offline; not deployed. Live extraction is still blocked by the Bedrock quota (see
  Consequences).
- **Date:** 2026-11-14
- **Implementation:**
  - State machine: `orchestration/step_functions/document_intelligence.asl.json`
  - Terraform: `infrastructure/live/dev/data_platform/document_intelligence.tf` (machine, role,
    EventBridge rule and target); the AI extraction role's trust and review-queue permissions
    (`main.tf`); `PROCURELAKE_AI_REVIEW_TABLE` on the task runner (`batch_and_quality.tf`); S3 to
    EventBridge on the artifacts bucket (`modules/s3_lakehouse`, `eventbridge_buckets`, set in
    `dev/foundation`); a failure alarm (`dev/observability`)
  - Task runner: task `extract-document` (`jobs/containers/task_runner/entrypoint.py`); the image
    now installs the `ai` dependency group and bundles `configs/ai` in the wheel
  - Python: `extract --skip-extracted` and `--role-arn` optional (`procurelake.ai.cli`),
    `review.existing_extraction`, `configs/ai` read through `resource_root()`
  - Make: `document-intelligence-run DOC=<id>`
  - Tests: `tests/unit/orchestration/test_document_intelligence_asl.py`, entrypoint, review queue,
    packaging, and Terraform tests (data_platform, s3_lakehouse, observability)

## Context
ADR 0032 built contract extraction (Textract + Claude Haiku 5.5 on Bedrock, deterministic checks,
a human review queue, Silver and Gold) as `make ai-*` commands. Open question 41 deferred a state
machine until Bedrock access worked, and left its trigger open: on landing, or on a schedule.

On 2026-11-14 the owner chose to build it now, started **when a document lands**. Live
extraction remains blocked (Consequences).

## Decision
1. **Trigger: the landing manifest, not the PDF.** `land_document` writes the PDF, the
   USAspending award record, then the manifest
   (`pipeline_manifests/public_contracts/contract_documents/window_start=snapshot/batch_id=<document id>-<sha256[:16]>/manifest.json`)
   in the artifacts bucket. The manifest is written last and only on success, so its creation
   means the document is complete. The artifacts bucket sends object events to EventBridge
   (`aws_s3_bucket_notification`, `eventbridge = true`, the provider attribute verified in the
   installed schema). A rule matches `Object Created` under that prefix and starts one execution.
2. **One execution per document.** `Route` accepts an S3 event (`detail.object.key`) or
   `{"document_id": "..."}` (`make document-intelligence-run DOC=…`); anything else fails
   (`BadInput`). Either value reaches the container only as an environment variable. The
   entrypoint accepts a manifest key only if it matches the exact pattern above and takes the
   document id from it. A document id given by hand must match the catalog's format. Nothing else
   reaches a command line.
3. **Extraction runs in the task runner as the AI extraction role.** `Extract` is
   `ecs:runTask.sync` with `TaskRoleArn` overridden to `procurelake-dev-ai-extraction`, the same
   pattern the data-quality machine uses for the data engineer (ADR 0040). The role's trust
   gains `ecs-tasks.amazonaws.com`, limited to this account (`aws:SourceAccount`). Its
   permissions are unchanged except `dynamodb:Query` on the review table. The machine may pass
   only that role and the runner's execution role, and only to ECS tasks.
4. **A repeated event costs nothing.** S3 delivers events to EventBridge at least once. The task
   runs `extract --skip-extracted`: if the review queue already holds an extraction of the same
   PDF bytes (SHA-256) with the same prompt and schema versions, in any status, the document is
   skipped. A new PDF, prompt or schema is extracted again. A failed extraction is never queued,
   so it is retried.
5. **Retries.** ECS API errors retry twice (30 s, doubling). A non-zero exit, most likely Bedrock
   throttling when several documents land together, retries twice, 2 minutes apart and doubling.
   A non-zero exit fails the task state (ADR 0029, observed live), and an explicit
   `ExitCode == 0` check follows as a second safeguard.
6. **The machine stops at the review queue.** On success it publishes "ready for review" to the
   pipeline alerts topic with the document and `make ai-review` / `make ai-decide`. On failure it
   publishes an alert and fails, and a CloudWatch alarm on failed, timed-out or aborted
   executions covers it like the other machines. Approval stays with a person (ADR 0032 §6):
   nothing reaches Silver or Gold until `make ai-decide`, and the Gold mart is rebuilt by the next
   dbt run.
7. **Packaging.** The task runner image installs the `ai` uv group (`anthropic[bedrock]==2.3.0`,
   `pypdf==6.21.4`, unchanged pins). The wheel bundles `configs/ai` (prompt, schema, model
   config, catalog, labels), read through the same `resource_root()` as the Silver contracts.
   The Glue wheel carries these 48 KB too. The image grew from 478 MB to 525 MB (local build).
8. **Can be turned off.** `document_intelligence_on_landing = false` removes the rule and keeps
   the machine for runs by hand. Like the other runner machines, nothing exists until the task
   runner image is deployed.

## Alternatives considered
- **Trigger on the PDF** (`documents/public_contracts/.../source.pdf` in the raw bucket). The
  manifest, which extraction needs, does not exist yet at that moment.
- **Daily schedule.** Fewer runs, but a document waits up to a day. Not chosen by the owner.
- **A separate container for AI.** A second image, repository and cluster for one task. The task
  runner already has the network, logging and override pattern.

## Consequences
- **Not deployed, and live extraction is still blocked.** On 2026-11-14 the account's quota for
  Claude Haiku 5.5 through the Canada profile (Service Quotas `L-47520EB7`, "Cross-region model
  inference tokens per minute for Anthropic Claude Haiku 5.5") was **0**, as was the global
  profile's (`L-F26CA8D1`). The AWS default is 10,000,000, the quota is adjustable, and the model
  reported `authorizationStatus: AUTHORIZED`. A test call still returned
  `AccessDeniedException: … not available for this account`. A Service Quotas request for
  100,000 was refused: "You must provide a quota value greater than the default quota value of
  1.0E7". The applied 0 is an account-level restriction, to be raised through an AWS Support case
  (owner, pending). Until then an execution fails at `Extract` and alerts.
- **Deploy order.** The foundation apply turns on S3 to EventBridge for the artifacts bucket.
  Then rebuild the task runner image (`make task-runner-image`, set `TASK_RUNNER_IMAGE_DIGEST`),
  then apply data_platform and observability. Landing a new document (`make ai-land DOC=…`) then
  starts an execution. A document landed before (same bytes) writes no new manifest; use
  `make document-intelligence-run DOC=…`.
- **Events from the whole artifacts bucket go to EventBridge.** Athena results, dbt artifacts and
  run summaries are written there. Only the matching rule starts anything. Whether these events
  are billed was not checked; review the first month's bill line for EventBridge.
- **The success notice uses the pipeline alerts topic.** Its subscriber is the operator, who is
  also the reviewer here. A separate review topic is a change if reviewers differ.
- **Concurrency.** Several documents landing together start several executions. With a 100,000
  tokens per minute quota and roughly 10,000 to 20,000 tokens per document (an estimate, not
  measured), throttling is possible. It is retried, then alerts, and a rerun skips what
  succeeded.
