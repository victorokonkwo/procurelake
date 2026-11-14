# ADR 0050: CDC hardening: verified TLS to the source, DMS and freshness alarm fixes, selective dbt builds

- **Status:** Accepted (owner decision 2026-11-14; resolves open questions 37, 39 and 40). Built
  and tested offline; not deployed; live verification needs a dev/cdc session.
- **Date:** 2026-11-14
- **Implementation:**
  - TLS: `src/procurelake/common/rds_tls.py`, the pinned bundle
    `configs/tls/rds-ca-central-1-bundle.pem`, `scripts/aws/rds_ca_check.py`
    (`make rds-ca-check`); every operator script that connects to the source
    (`scripts/aws/cdc_{source_setup,workload,freshness,failure_drill,stall_drill}.py`,
    `scripts/aws/demo.py`) and `tests/e2e/test_cdc_to_gold.py`
  - Alarms: `infrastructure/live/dev/cdc/main.tf` (the stopped rule, the latency alarm's missing
    data), `infrastructure/live/dev/observability/main.tf` (the freshness alarm's description);
    `scripts/aws/cdc_stall_drill.py` detects through the stopped rule
  - Selective dbt: `src/procurelake/cdc/checkpoint.py` (the Gold build queue),
    `orchestration/step_functions/{cdc_processing,dbt_transformation}.asl.json`,
    `jobs/containers/dbt_runner/entrypoint.py`, `infrastructure/live/dev/data_platform/main.tf`
    (IAM, the daily full build schedule)
  - Tests: `tests/unit/common/test_rds_tls.py`, `tests/unit/cdc/test_checkpoint.py`,
    `tests/unit/orchestration/{test_cdc_processing_asl,test_dbt_runner_entrypoint}.py`, and the
    Terraform tests of dev/cdc, dev/data_platform and dev/observability
  - Runbooks: `docs/runbooks/cdc-dms-stall.md`, `docs/runbooks/pipeline-alarms.md`,
    `docs/runbooks/demo.md`

## Context
Three findings from the CDC work were left open:
- **Q37 (ADR 0027).** The operator scripts connected to the RDS source with `sslmode=require`:
  encrypted, but the server certificate was not checked, so a machine in the path could present
  any certificate.
- **Q39 (ADR 0031, the Week 8 stall drill).** (a) The DMS latency alarm went to ALARM 11.8
  minutes after the task stopped; the EventBridge state-change event arrived within a minute.
  (b) The same alarm fires once when the stack is created, before the task publishes metrics.
  (c) The freshness alarm cleared by itself about 15 minutes after a late batch, with no newer
  batch, so its OK did not mean a later batch was on time.
- **Q40 (Week 10 benchmark §3).** A `cdc_processing` run took about 7.5 minutes (apply about
  2.5, a full dbt build about 5), longer than the 5-minute schedule. Every other tick was skipped
  and the worst case from commit to Gold was about 17.5 minutes at any volume. 5,929 changes
  committed over 6 minutes missed the 15-minute gate.

On 2026-11-14 the owner decided: pin the RDS CA bundle and verify the host (`verify-full`);
apply all three alarm fixes; build in the CDC path only the dbt models downstream of the Silver
tables that changed, keeping a full build elsewhere.

## Decision

### 1. `verify-full` against a committed, pinned RDS CA bundle (Q37)
- **Bundle.** The AWS RDS User Guide ("Using SSL/TLS to encrypt a connection to a DB instance or
  cluster", certificate bundles by AWS Region) lists
  `https://truststore.pki.rds.amazonaws.com/ca-central-1/ca-central-1-bundle.pem` for Canada
  (Central). Downloaded 2026-11-14 (twice, same bytes): 4,604 bytes, SHA-256
  `7e458e5489c48a92217efbe527da1203dc0fddebd802261fae9eed85fedb1e9f`, three root certificates:
  Amazon RDS ca-central-1 Root CA RSA2048 G1, RSA4096 G1 and ECC384 G1. The guide says the
  regional bundle holds the `rds-ca-rsa2048-g1`, `rds-ca-rsa4096-g1` and `rds-ca-ecc384-g1`
  roots and that `rds-ca-rsa2048-g1` is the default CA. The instance does not set
  `ca_cert_identifier`, so it uses the default, which this bundle covers.
- **Committed, not downloaded at use.** The bundle is public. Committing it
  (`configs/tls/rds-ca-central-1-bundle.pem`, with a `.gitignore` exception for exactly that
  file) means a connection needs no network call besides the database, cannot be changed by a
  compromised or unavailable download, and is reviewable in Git history. The pin is
  `rds_tls.BUNDLE_SHA256`. Before every connection the file's SHA-256 is checked; a mismatch
  refuses to connect.
- **Rotation.** AWS adds or rotates root CAs rarely, but it does. `make rds-ca-check` downloads
  the bundle again and compares it, and the committed file, with the pin (no AWS credentials). A
  difference is reviewed by hand before re-pinning; the script never rewrites the file.
- **Connection.** `rds_conninfo()` builds the libpq string with `sslmode=verify-full` and
  `sslrootcert=<bundle>`. verify-full also checks the host name against the certificate, so the
  host must be the endpoint name: the function refuses an IP address and any name that is not
  `<…>.ca-central-1.rds.amazonaws.com`. The Makefile already passes `source_address` (the
  `aws_db_instance.address` attribute, the endpoint host name).
- **Scope.** The operator scripts and the live acceptance test. DMS's own source endpoint keeps
  `ssl_mode = "require"` (ADR 0027): `verify-full` there needs the CA imported as a DMS
  certificate and was not part of this decision.

### 2. Alarm fixes (Q39)
- **(a) A rule for the task stopping or failing.** `procurelake-dev-cdc-dms-task-stopped` in
  dev/cdc, next to the task, created and destroyed with it, publishing to the pipeline alerts
  topic with an "ALARM … stopped or failed" message and the runbook. The topic policy already
  admits `events.amazonaws.com` for rules named `procurelake-dev-*` in this account, and the data
  key admits it through SNS (ADR 0031), so no policy changes.
  - **The pattern, and what the docs do not say.** The AWS DMS User Guide ("Working with Amazon
    EventBridge events and notifications in AWS DMS") gives the event shape: `source` `aws.dms`,
    `detail-type` `DMS Replication Task State Change`, the task ARN in `resources`, and `detail`
    with `type`, `category`, `eventType` and `detailMessage`. Its only sample is a start
    (`eventType` `REPLICATION_TASK_STARTED`, category `StateChange`). Its ReplicationTask table
    lists `DMS-EVENT-0079` "Replication task stopped" (StateChange) and `DMS-EVENT-0078` "A
    replication task has failed" (Failure). It shows no stopped or failed sample, so the stop's
    `eventType` value and the field that carries the event ID are not documented. The rule
    therefore uses `$or` (EventBridge comparison operators) on `detail`: `category` `Failure`,
    or `eventType` `REPLICATION_TASK_STOPPED` / `REPLICATION_TASK_FAILED` (the documented
    `REPLICATION_TASK_<STATE>` form), or `eventId` `DMS-EVENT-0079` / `DMS-EVENT-0078`. A
    branch whose field is absent never matches. EventBridge `TestEventPattern` (2026-11-14, an
    API that creates nothing) accepted the pattern, matched constructed events carrying any one
    of the three, and did not match the documented start sample. Those events are constructed,
    not captured from DMS.
  - **Fallback kept.** The existing rule for every task state change stays. In the drill it
    delivered the stop within a minute, so a stop is reported even if the new pattern turns out
    not to match.
  - **Best effort.** The EventBridge events reference lists DMS events as best-effort delivery.
- **(b) The latency alarm no longer treats missing data as breaching.** `treat_missing_data` is
  now `missing`. CloudWatch's documentation: with `missing`, if every datapoint in the
  evaluation range is missing the alarm goes to INSUFFICIENT_DATA. The alarm has no
  INSUFFICIENT_DATA action, so stack creation (no metrics yet) sends nothing. A running task
  that lags still alarms after 5 minutes over 300 s.
  - **Trade-off.** A stopped task also publishes nothing, so the latency alarm no longer reports
    a silent stop; it shows INSUFFICIENT_DATA on the dashboard instead. Detection of a stop now
    rests on (a), which was the faster signal in the drill (within a minute against 11.8
    minutes) but is best effort. If DMS stops without emitting an event, nothing alerts until a
    later change is late (the freshness alarm, once DMS resumes). The alternatives were keeping
    `breaching` and accepting the creation alarm, or a composite alarm; the owner chose to rely
    on the event.
  - **The 11.8-minute delay is not explained here.** The CloudWatch docs say an alarm retrieves
    more datapoints than its evaluation periods (the evaluation range) and fills missing ones as
    few times as possible, which is consistent with a longer delay. It was not measured further.
- **(c) The freshness alarm's OK is documented as "time passed".** The description of
  `procurelake-dev-cdc-source-to-silver-latency` and the alarms runbook now say that OK does
  not mean a newer batch arrived or was on time, and to check the `SourceToSilverSeconds`
  datapoints. The alarm itself is unchanged (missing data not breaching: no batch is not an
  alarm).
- **The stall drill** (`make cdc-stall-drill`) now waits for the stopped rule's
  `AWS/Events` `Invocations` (and records `MatchedEvents`, `FailedInvocations` and the latency
  alarm's state). If the rule does not fire within 20 minutes the drill records the failure
  and still resumes DMS.

### 3. Selective dbt builds in the CDC pipeline (Q40)
- **Which tables changed: a queue next to the checkpoint.** The CDC apply job writes no run
  summary, and `glue:startJobRun.sync` returns no job output. So each applied batch adds
  `<silver table>#<batch id>` to a string set on one DynamoDB item (`pk = gold#cdc`,
  `sk = pending`) in the checkpoint table, in the **same `TransactWriteItems`** that advances
  that table's checkpoint. A batch is therefore never in Silver without being queued, and a
  writer that lost its lease queues nothing.
- **`cdc_processing`.** After the apply it reads the item (`dynamodb:getItem`, consistent read).
  - Nothing queued: it succeeds without running dbt.
  - Otherwise it starts `dbt_transformation` with `changed_silver`, the entries as a JSON
    string (`States.JsonToString`).
  - After that build (and its tenant access check) succeeds, it removes exactly the entries it
    read (`dynamodb:updateItem`, `DELETE`). An entry queued during the build has another batch
    id and stays.
  - A failed build, or a failed apply, leaves the queue as it was, so the next run builds those
    tables. Before this, the next full build repaired Gold; now the queue does.
- **`dbt_transformation`.** Its input is merged over defaults (`run_id` = the execution name,
  `changed_silver` = `all`), and `changed_silver` reaches the container as
  `PROCURELAKE_DBT_CHANGED_SILVER`. The tenant access check still follows every build.
- **The runner builds the selector, strictly.** The value is parsed as JSON, never passed to a
  shell. Every entry must match `^<table>#<batch id>$` with restricted characters, and every
  table must be a table of the dbt source `silver`, read from its sources file in the image
  (a unit test checks that every CDC-fed Silver table is in it). Anything else fails the run.
  It cannot be combined with another command or a selection in `PROCURELAKE_DBT_ARGS`, and a
  full refresh stays refused. The command is
  `dbt build --select source:silver.<table>+ …` (the dbt docs' `source` method with the `+`
  graph operator). `dbt build` runs the tests of selected nodes; with dbt's default eager
  indirect selection that includes tests that also reference unselected models. More than 40
  entries (failed builds piling up) means a full build.
- **Checked offline** with `dbt ls` on the project (dbt-core 1.12.6): the project has 19 models
  and 90 tests. `source:silver.<table>+` selects 1 model and 5 tests for `silver_tenants`, 2/7
  for agencies and suppliers, 6/36 for solicitations and awards, 5/26 for purchase orders and
  4/20 for invoices. Several values after one `--select` are accepted.
- **The full build stays, daily.** No other scheduled run did a full build: `data_quality` runs
  `dbt test` only, and batch ingestion stops at Silver. A new EventBridge Scheduler schedule,
  `procurelake-dev-dbt-full-build`, starts `dbt_transformation` with `changed_silver = "all"` at
  07:40 America/Toronto (`schedule_dbt_full_build`; empty disables it): after the 07:00
  USAspending ingestion and before the 08:00 data-quality `dbt test`. It runs whether dev/cdc
  is up or not, and rebuilds the models no CDC table feeds (USAspending reference and AI marts).
  `make dbt-build` remains a full build too.
- **IAM.** The apply job gains `dynamodb:UpdateItem` on the checkpoint table (a transaction's
  Update is authorized as UpdateItem). `cdc_processing` gains `dynamodb:GetItem` and
  `dynamodb:UpdateItem` on that table, limited by `dynamodb:LeadingKeys` to `gold#cdc`. Its
  existing `kms:Decrypt` / `kms:GenerateDataKey` on the data key covers DynamoDB's use of the
  key on its behalf. The new scheduler role may start only `dbt_transformation`.

## Alternatives considered
- **Download the bundle at every use and check the checksum.** Same protection, plus a network
  dependency on every connection; no benefit over committing a public file.
- **Pass the changed tables from the Glue job's output.** The `.sync` integration returns the
  job run, not the script's output, and a separate run summary in S3 would need its own
  "built" bookkeeping. The queue in the checkpoint transaction is atomic with the apply.
- **Build only what this run applied.** Simpler, but a failed dbt build would leave its tables
  stale in Gold until the daily full build, because the next run would apply nothing new.
- **Start each run when the previous ends**, or **relax the gate** (Q40's other options). Not
  chosen by the owner.

## Consequences
- **Not deployed; nothing here is measured live.** Whether the selective build brings the C5
  gate back under 15 minutes is a design target until `make cdc-freshness` (and the scheduled
  variant at the Week 10 volumes) has run on dev/cdc. That needs the billed stack and the
  owner's approval. The model and test counts above are from `dbt ls`, not timings.
- **Unverified until a live stop:** that `procurelake-dev-cdc-dms-task-stopped` matches a real
  stop event (the run of `make cdc-stall-drill` must show its MatchedEvents and Invocations);
  that the latency alarm stays quiet at stack creation; and that `verify-full` connects to the
  RDS instance (the first `make cdc-source-setup` of the next session).
- **Concurrent builds.** The daily full build and a CDC run's build can overlap while dev/cdc is
  up at 07:40. Two dbt builds then write the same Iceberg tables, and one may fail on a commit
  conflict and alert. A failed CDC build keeps its queue for the next run; a failed daily build
  waits for the next day or `make dbt-build`. Not guarded, and not observed or measured.
- **Deploy order.** Rebuild the dbt runner image (`make dbt-image`) so it has the selection code,
  then apply data_platform: the new `cdc_processing` sends `changed_silver`, which an older image
  would ignore and answer with a full build. The new apply job (wheel) and state machines go in
  the same data_platform apply. dev/cdc changes on its next apply; dev/observability changes only
  the freshness alarm's description.
- **Cost.** The daily full build is one Fargate task (0.5 vCPU, 1 GiB) of about 5 minutes plus
  its Athena scans, each day, CDC up or not. Not measured separately. The DynamoDB reads and
  writes are a few per run.
