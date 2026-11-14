# ADR 0048: Glue dependency wheels in S3, and Glue job logs encrypted with the data key

- **Status:** Accepted (owner decision 2026-11-14; resolves open questions 32 and 33). Built and
  tested offline; not deployed.
- **Date:** 2026-11-14
- **Implementation:**
  - Bundle builder: `src/procurelake/lakehouse/glue_dependencies.py` (standard library only),
    run by `make glue-wheel`, which now writes `dist/procurelake-0.1.0-py3-none-any.whl` and
    `dist/procurelake-glue-dependencies.gluewheels.zip`. CI runs the same target before planning
    (`scripts/ci/terraform_dev.sh`)
  - Terraform module `infrastructure/modules/glue_spark_job/`: uploads the bundle
    (`aws_s3_object.dependencies`), installs with `--no-index --no-deps`, attaches the security
    configuration, creates the job's two log groups with the KMS key, grants
    `logs:AssociateKmsKey` and the key through CloudWatch Logs. The `python_modules` and
    `log_group_arns` inputs are replaced by `dependencies_bundle_path`,
    `security_configuration_name` and `log_kms_key_arn`
  - `infrastructure/modules/kms/`: optional `cloudwatch_logs_log_group_patterns` key policy
    statement
  - `dev/foundation`: the security configuration's name (output
    `glue_security_configuration_name`) and the key policy statement for its log groups
  - `dev/data_platform`: `aws_glue_security_configuration.jobs`, passed to all six Glue jobs
    (bronze-to-silver, apply-cdc, iceberg-maintenance, reference-to-silver, the bench Silver
    job and partition evolution)
  - `bootstrap`: the CI deploy role may create and delete Glue security configurations
  - Tests: `tests/unit/lakehouse/test_glue_dependencies.py`, `test_glue_packaging.py`; Terraform
    tests (glue_spark_job, kms, foundation, data_platform, bootstrap)
  - Runbook: `docs/runbooks/iceberg-maintenance.md` (new log group names)

## Context
ADR 0025 installs the procurelake wheel from S3 and the exact pins of
`jobs/glue/silver_python_modules.txt` from PyPI at the start of every Glue run, with
`--no-deps`. A PyPI outage, a yanked file or a network change would stop every Silver job,
and what is installed is only as fixed as pip's resolution of `name==version` on that day.
Open question 32 asked whether to host the wheels in the artifacts bucket.

The Glue jobs log to `/aws-glue/jobs/{error,output}` with CloudWatch Logs' default encryption,
while every other store in the platform uses the dev data key. Open question 33 asked whether
to encrypt the job logs with it.

On 2026-11-14 the owner chose both.

## Decision

### 1. Dependency wheels: one zip of wheels per build, installed with no index
1. **The documented mechanism.** AWS Glue 5.0 and later accept a zip of wheels with the
   `.gluewheels.zip` suffix in `--additional-python-modules`. With
   `--python-modules-installer-option --no-index` the zip "acts as a local index for pip to
   resolve dependencies from at runtime", and PyPI is not used ([AWS Glue docs, "Using Python
   libraries with AWS Glue", "Installing additional Python libraries in AWS Glue 5.0 or above
   using Zip of Wheels"](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-programming-python-libraries.html#glue-python-library-installing-zip-of-wheels)).
   AWS recommends this form over `name==version` lists for production jobs on the same page.
2. **Platform.** The same page (Appendix B) gives Glue 5.1 as Python 3.11 on Amazon Linux 2023,
   glibc 2.34, with the compatible tags `manylinux_2_34_x86_64`, `manylinux_2_28_x86_64` and
   `manylinux2014_x86_64`. Glue 5.1 workers are x86_64; the Appendix A example builds with
   `--platform manylinux2014_x86_64 --python-version 311`. The builder accepts `py3-none-any`,
   `cp311-cp311` and `cp3x-abi3` (x ≤ 11) wheels for `any` or an x86_64 manylinux tag whose glibc
   is at most 2.34 (PEP 600: `manylinux2014` is `manylinux_2_17`). It rejects aarch64, musllinux,
   macOS, Windows, other CPython versions and newer glibc. Where several wheels fit, it prefers
   the interpreter-specific build, then the newest glibc.
3. **Source: `uv.lock`, verified.** For each pin the builder takes the package locked at exactly
   that version, picks the Glue 5.1 wheel from the lock's wheel list, downloads it from
   `files.pythonhosted.org` (the only host accepted) and checks it against the lock's SHA-256.
   Wheels are cached under `build/glue_wheels_cache/<sha256>/` and checked again on reuse. A pin
   that is not locked, or has no Glue 5.1 wheel, stops the build. The selection today:
   `annotated_types-0.8.0-py3-none-any`, `pydantic-2.14.3-py3-none-any`,
   `pydantic_core-2.50.4-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64`,
   `pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64`,
   `typing_extensions-4.16.0-py3-none-any`, `typing_inspection-0.4.5-py3-none-any`.
4. **Layout and reproducibility.** As in the docs' example, the wheels and a `requirements.txt`
   of the exact pins sit under `wheels/`. Entries are sorted and stored uncompressed (wheels are
   already compressed, and stored entries do not depend on the zlib version), with a fixed
   1980-01-01 timestamp and 0644 permissions. Two builds from an empty cache on 2026-11-14 gave
   the same SHA-256 (`2c9b4451…0f65`, 3,629,472 bytes). This matters because CI re-plans before
   apply and compares plan fingerprints (ADR 0035): the bundle's key contains its SHA-256.
5. **Job arguments.** `--additional-python-modules` is the wheel's S3 URI, then the bundle's;
   no `name==version` entry remains. `--python-modules-installer-option` is
   `--no-index --no-deps`. `--no-deps` stays because the procurelake wheel declares
   `boto3>=1.43.127`, `psycopg` and `requests>=2.34.2`: Glue 5.1 ships boto3 1.40.61 and
   requests 2.32.5, and the jobs use neither psycopg nor requests (ADR 0025, guarded by
   `test_job_imports_without_psycopg_or_requests`). Resolving them without an index would fail.
   The job records `--dependencies-sha256` next to `--wheel-sha256`.
6. **Storage.** The bundle is uploaded per job like the wheel
   (`glue/wheels/<job>/<sha256>/procurelake-glue-dependencies.gluewheels.zip`), so each object
   has one owner; the job role may read only its own key.

### 2. Job logs: a Glue security configuration with CloudWatch Logs encryption
1. **One security configuration** `procurelake-dev-glue-jobs` in data_platform, attached to all
   six Glue jobs. CloudWatch encryption is `SSE-KMS` with the dev data key. S3 encryption is
   `DISABLED`: Glue then sets no encryption on what it writes and the buckets' default
   encryption (SSE-KMS with the same key, `modules/s3_lakehouse`) applies as before, so data
   encryption does not change. Job bookmarks are off in every job, so bookmark encryption is
   `DISABLED`. The provider requires all three blocks; the modes are those of the Glue API
   ([Security APIs](https://docs.aws.amazon.com/glue/latest/dg/aws-glue-api-jobs-security.html)).
   The name comes from foundation, which owns the key policy that names its log groups.
2. **Log group names change.** With CloudWatch encryption in a security configuration, Glue
   writes to `/aws-glue/jobs/<security configuration>-role/<job role>/error` and `/output`
   ([AWS Glue docs, "Logging for AWS Glue jobs", "Log group naming with security
   configuration"](https://docs.aws.amazon.com/glue/latest/dg/monitor-continuous-logging.html#monitor-log-group-naming)).
   Each job role is named like its job, so each job now has its own two groups, for example
   `/aws-glue/jobs/procurelake-dev-glue-jobs-role/procurelake-dev-bronze-to-silver/error`. The
   module creates them before the first run with the data key and 30-day retention, and the job
   role may write only to its own.
3. **Job role permissions.** `logs:AssociateKmsKey` on its log groups: the same page says that
   without it continuous logging is disabled. Its resource type is the log group, and
   `CreateLogGroup`/`PutLogEvents` keep theirs (AWS Service Authorization Reference for CloudWatch
   Logs, read from the programmatic reference `servicereference.us-east-1.amazonaws.com/v1/logs/logs.json`).
   The key through CloudWatch Logs only (`kms:ViaService = logs.ca-central-1.amazonaws.com`):
   `kms:Encrypt`, `kms:ReEncrypt*`, `kms:Decrypt`, `kms:GenerateDataKey*`, `kms:Describe*`, the
   set the CloudWatch Logs docs give for writing encrypted log data.
4. **Key policy.** The data key gains the statement from the CloudWatch Logs docs: principal
   `logs.ca-central-1.amazonaws.com`, the same five actions, limited by
   `ArnLike kms:EncryptionContext:aws:logs:arn` to
   `arn:aws:logs:ca-central-1:<account>:log-group:/aws-glue/jobs/procurelake-dev-glue-jobs-role/*`
   ([CloudWatch Logs docs, "Encrypt log data in CloudWatch Logs using AWS
   KMS"](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html),
   step 2). The Glue setup page asks for the same service statement
   ([Setting up encryption in AWS Glue](https://docs.aws.amazon.com/glue/latest/dg/set-up-encryption.html)).
   The root statement, which delegates the job roles' use of the key to IAM, is unchanged.
5. **The shared groups are kept.** `/aws-glue/jobs/{error,output}` stay in data_platform with
   30-day retention so logs of earlier runs stay readable; Glue no longer writes to them once
   the jobs use the security configuration, and the job roles lose write access to them. They
   can be removed in a later change.
6. **CI deploy role.** Glue security configurations have no resource type (Service Authorization
   Reference), so `glue:CreateSecurityConfiguration`, `glue:DeleteSecurityConfiguration` and
   `glue:GetSecurityConfiguration` are added to the deploy role's unscoped actions. The new log
   groups are under `/aws-glue/jobs/*`, already in its scope (ADR 0036). The plan role's
   `ReadOnlyAccess` includes `glue:GetSecurityConfiguration(s)` (AWS managed policy reference).

## Alternatives considered
- **List each dependency wheel as its own S3 URI** (also documented). The same packages and
  `--no-index`, but six more objects per job and no single artifact to hash. Kept as the
  fallback if the combined form in §1.5 does not install on the first run.
- **A Python virtual environment** (`--python-virtual-env`, Glue 5.0 and later). It replaces the
  whole Glue Python environment, so boto3 and everything else Glue provides would have to be
  rebuilt and pinned. More to maintain for two packages and their closure.
- **Put the procurelake wheel inside the zip.** One object, but the dependency bundle would then
  change with every code change, and the wheel's declared requirements would still need
  `--no-deps`.
- **A separate KMS key for logs.** The CloudWatch Logs docs recommend a key per log group. The
  dev platform uses its one data key for buckets, RDS, DMS output, DynamoDB and SNS (ADR 0019,
  0027, 0031); the key policy statement is limited to the Glue job log groups instead.
- **`--custom-logGroup-prefix`** to keep names stable. Glue still inserts
  `<security configuration>-role/<role>/` under a custom prefix (same page), so names change
  either way.

## Consequences
- **Not deployed; to confirm on the first dev run.** What was verified offline: two builds give
  the same bytes; the bundle installs with `pip install --no-index --no-deps -r
  wheels/requirements.txt --find-links wheels` plus the procurelake wheel in an
  `amazonlinux:2023-minimal` linux/amd64 container (emulated on arm64; AL2023's Python 3.11 and
  pip 22.3.1; Glue 5.1 has pip 24.0), and pydantic, pydantic-core and PyYAML's C loader import.
  Glue's own install command for a zip of wheels is not documented, and the docs show the zip
  alone, not next to a wheel URI with `--no-deps` added. The first run of each job must show in
  its output log group that the six packages were installed with no PyPI access and that the
  run passed; if not, switch to the per-wheel form above.
- **Deploy order.** Bootstrap first (owner, by hand: deploy role permissions), then foundation
  (key policy), then data_platform. CI applies foundation before data_platform. Creating a log
  group with a KMS key fails if CloudWatch Logs cannot use the key, and data_platform's plan
  fails until foundation outputs `glue_security_configuration_name`, so the order is enforced.
- **Reading job logs needs the key.** Anyone reading the new groups (console, `aws logs`) needs
  `kms:Decrypt` on the data key through CloudWatch Logs. The operator's own permissions were not
  checked. No alarm, metric filter, dashboard, script or Make target in the repository reads the
  Glue log groups; the only reference was the Iceberg maintenance runbook, updated.
- **Glue dependency updates** now need `make glue-wheel` (CI does it) after changing
  `silver_python_modules.txt` and `uv.lock`; nothing is fetched at run time. Security updates of
  these packages are ours (AWS shared responsibility note on the same docs page); `make
  security-scan` audits `uv.lock`, which the bundle is built from.
- **Storage.** About 3.6 MB per job and build in the artifacts bucket (six jobs).
- **Plan impact (expected, not run: no credentials used).** foundation: the KMS key policy is
  updated in place. data_platform: one security configuration and twelve log groups created;
  six bundle objects created; six Glue jobs (`UpdateJob` accepts `SecurityConfiguration`) and
  six runtime role policies updated in place. The wheel now contains the builder module, so its
  SHA-256 and the six wheel object keys change: those objects are replaced, as on any code
  change. The shared log groups are unchanged. bootstrap: the deploy role policy is updated in
  place.
