# Security review, dev (2026-11-12)

Work package G, "security review". Scope: the deployed `dev` environment in account
697191766499, `ca-central-1`, and the repository that builds it.

Every control below is linked to the ADR that decided it and the evidence that shows it working.
A control with no live evidence says so. The tenants `tenant_alder` and `tenant_birch` are
fictional, and all tenant data is synthetic.

## 1. What is protected, from whom

| Asset | Where | Threats considered |
|---|---|---|
| Tenant procurement data (synthetic) | Silver and Gold Iceberg tables in the lake bucket; tenant exports in the products bucket | One tenant reading another's rows; reading around Lake Formation through S3; a mart rebuild silently opening access |
| Raw history and audit trail | Bronze (versioned), DMS CDC files, run manifests, DynamoDB checkpoints and review decisions | Deletion or overwrite; a teardown that destroys evidence |
| Credentials | RDS master and replication secrets, CI identity, operator key | Secrets in Git; long-lived CI keys; over-broad deploy rights |
| Infrastructure | Terraform state; six roots | State tampering; CI deploying anything to the account |
| AI outputs | Contract-intelligence Silver table and review queue | Unreviewed model output published as fact |

Out of scope for dev: multi-account isolation (ADR 0011), high availability, DDoS, and insider
threat beyond the single operator.

## 2. Controls and evidence

### Tenant isolation (the main risk)
| Control | Decided in | Evidence |
|---|---|---|
| Lake Formation row filter per tenant per mart (`tenant_id = '<tenant>'`), hybrid mode on Gold | ADR 0030 | `make test-aws`: 27 passed on 2026-11-12, after the ADR 0037 changes |
| Explicit deny on `s3:*` for the lake and raw buckets on tenant and admin roles, so no path around Lake Formation | ADR 0008, 0030 | Denied-path tests in `make test-aws` |
| A workgroup and results prefix per tenant | ADR 0030 | Same suite; concurrency benchmark ran in separate workgroups |
| Isolation holds when both tenants query at once | This review | `docs/benchmarks/2026-11-12-replay-concurrency-cdc.md` §2: 48 queries, every result held only the querying tenant's rows |
| Exports run with the tenant's own credentials, so the filter applies; bucket policy confines each tenant to its prefix and denies deletes | ADR 0030 | Export row counts equal the tenant's query counts; `test_exports_hold_only_the_tenants_rows` |
| Marts are merged, never dropped, so filters cannot vanish in a rebuild; the dbt runner refuses `--full-refresh` | ADR 0030 | Measured failure that led to it (fail-closed, no rows leaked) |
| Detective control: daily tenant access check alerts on any cross-tenant row | ADR 0030, 0031 | `make tenant-access-drill`: the check failed and alerted on an injected grant, then passed after revoke |
| Tenant role trust limited to named principals (operator and the two state-machine roles) | ADR 0037 | Unit test `tenant_and_admin_roles_trust_only_named_principals`; applied 2026-11-12, exports and access check still succeed |

### Identity and deployment
| Control | Decided in | Evidence |
|---|---|---|
| GitHub Actions authenticates with OIDC; no AWS keys in CI | ADR 0010, 0019 | `deploy-dev` runs, for example 43114073852 |
| Scoped deploy role: `ReadOnlyAccess` plus writes on `procurelake-dev-*` only, with explicit denies on managed-policy attachment, identities, the CI roles, lake objects and the state key | ADR 0036 | First IAM and EC2 writes through it, 2026-11-12 |
| Applies need the owner's approval on the `dev` environment; plans are fingerprint-checked | ADR 0035 | Verified end to end (ADR 0035) |
| Workload roles per job (Silver, CDC apply, maintenance, dbt, ingestion, AI extraction), scoped to their prefixes and tables | ADR 0025, 0028, 0029, 0033 | Live runs as each role |

### Data protection
| Control | Decided in | Evidence |
|---|---|---|
| SSE-KMS with a customer-managed key on lake buckets; separate key for state | ADR 0019 | Bucket encryption verified live (ADR 0019); `alias/procurelake-dev-data`, `alias/procurelake-tfstate` |
| Public access blocked on every bucket | ADR 0019 | Checkov plus live check (ADR 0037 waivers note) |
| Bronze is versioned, never expires, and writers cannot delete | ADR 0002 | Lifecycle rules; IAM deny |
| Secrets in Secrets Manager (the RDS master secret is RDS-managed; DMS reads the replication secret through an interface endpoint) | ADR 0027 | `cdc-source-setup` 2026-11-12: the DMS user can read the 8 source tables and write none, and is not a superuser |
| TLS forced on RDS; RDS reachable only from the operator's address; DMS in private subnets, no NAT | ADR 0027 | `operator_cidrs` checked against the current address before each session |
| The checkpoint advances only after a validated Iceberg write; per-table lease | ADR 0028 | CDC tests; 2026-11-12 run: the job refused a concurrent start |
| `destroy-dev` cannot touch state, foundation, the DynamoDB tables or logs | ADR 0038 | Dry run: 126 deletes, none of them kept resources |

### AI governance
| Control | Decided in | Evidence |
|---|---|---|
| Evidence span per field, schema validation, prompt and model version recorded, human review before publishing | ADR 0032 | Unit tests. **No live extraction yet** (Bedrock quota, Week 9) |

### Supply chain and secrets in code
| Control | Decided in | Evidence |
|---|---|---|
| Checkov on all Terraform, pip-audit on every locked group, gitleaks on full history, in CI and weekly | ADR 0037 | 2026-11-11: Checkov 0 failed after fixes and waivers; pip-audit no known vulnerabilities; gitleaks one false positive, waived |
| Pre-commit blocks private keys and AWS keys in staged files | ADR 0022 | Hook config |
| Pinned versions: `uv.lock`, Terraform lock files, dbt packages | ADR 0015, 0019, 0026 | Lock files in Git |

## 3. Residual risks (accepted for dev, to resolve before production)

| # | Risk | Why it is accepted now | What closes it |
|---|---|---|---|
| R1 | Operator uses an IAM user with a long-lived key and `AdministratorAccess` | Single-operator dev; the key is local, git-ignored | Open question 29: IAM Identity Center, remove the admin policy, rotate or delete the key |
| R2 | The deploy role can write inline policies (`iam:PutRolePolicy`) on `procurelake-dev-*` roles and has broad `lakeformation:*` | Needed to manage the workload roles and grants; applies need owner approval | Permissions boundaries on workload roles (ADR 0036 follow-up) so an inline policy cannot exceed the boundary |
| R3 | Bronze immutability relies on versioning and an IAM deny, not Object Lock | Object Lock must be set at bucket creation; dev data is synthetic or public | Open question 28 |
| R4 | Operator scripts use `sslmode=require` without verifying the RDS certificate | The source is reachable only from the operator's address | Open question 37: `verify-full` with the RDS CA bundle |
| R5 | Tenant roles can read Glue metadata (table names and schemas, not rows) for every Gold table | No rows are exposed; schemas are not tenant data | Open question 38 |
| R6 | Glue and CloudWatch logs use default encryption, not the customer-managed key | Logs hold job output, not tenant rows | Open question 33 |
| R7 | The repository-wide Checkov waivers (no S3 access logs, no flow logs, 30-day log retention, single region) | Dev scope and cost; listed in ADR 0037 | Revisit the ADR 0037 table for production |
| R8 | Lake Formation hybrid mode: the pipeline keeps IAM access to Gold | Tenants are opted in and governed; pipeline roles are not consumers | A full Lake Formation mode for every principal before more consumers are added |
| R9 | Cross-tenant admin product reads every tenant's rows | It is the intended product, in its own role and workgroup with the S3 deny | Owner review of who may assume `analytics-admin` |

## 4. Not reviewed
- No penetration test or third-party review was done.
- **No CloudTrail trail exists** (`describe-trails` returned none, 2026-11-12). Only the
  default 90-day management-event history is available, with no S3 data events and no
  long-term copy. A trail to a separate log bucket is a production prerequisite.
- The AI extraction path has not run live (Week 9).
