# Cost review, dev (2026-11-12)

Work package G, "cost review". Every figure is **measured**, from AWS's own records. List prices
are labelled as such and are only used to convert measured usage. Account 697191766499 is
dedicated to this project, so its bill is the project's cost.

Sources:
- **AWS Cost Explorer:** 2026-11-01 to 2026-11-13, unblended, read 2026-11-12 ~10:45 UTC. Figures
  are estimated and lag by up to a day, so the 2026-11-12 runs are not in them yet.
- **Glue `GetJobRuns`:** `DPUSeconds` for every run of every `procurelake-dev-*` job.
- **CloudWatch `AWS/Athena` `ProcessedBytes`:** successful DML only, per workgroup,
  2026-11-09 to 2026-11-13.

## 1. Spend to date

**Total: $3.26** (Cost Explorer, 2026-11-10 to 2026-11-11). This covers the foundation, Bronze
ingestion, Silver, Gold, two CDC sessions, the tenant products, the 100k and 1M benchmarks and
Iceberg maintenance.

| Usage type (ca-central-1) | Quantity | Cost (USD) |
|---|---|---|
| Glue ETL | 5.22 DPU-hours | 2.297 |
| DMS `dms.t3.small` | 6 h | 0.240 |
| Athena data scanned | 0.0293 TB billed | 0.161 |
| RDS `db.t4g.micro` | 6.45 h | 0.116 |
| S3 requests (Tier 1 + Tier 2) | 16,926 + 198,326 | 0.180 |
| VPC interface endpoint (CDC) | 6 h | 0.066 |
| KMS keys and requests | 2 keys (prorated), 25,048 requests | 0.087 |
| Public IPv4 (RDS) | 7.6 h | 0.038 |
| CloudWatch alarms | prorated | 0.026 |
| RDS gp3 storage | 0.18 GB-month | 0.023 |
| Fargate ARM (dbt runner) | 0.46 vCPU-h, 0.91 GB-h | 0.020 |
| Secrets Manager, DynamoDB, ECR, S3 storage | | < 0.01 each |
| Bedrock | 3 output tokens (quota probe) | 0.000001 |
| Textract | none billed | 0 |

**Glue is 70% of spend**, and the CDC stack (DMS, RDS, its storage, endpoint, IPv4) is 15%. Nothing else
reaches 6%.

## 2. Cost drivers, measured

### Glue (DPU-hours from job runs, all G.1X × 2 workers, Glue 5.1)

| Job | Runs | DPU-hours | At the $0.44 list price |
|---|---|---|---|
| `bench-bronze-to-silver` | 7 | 3.563 | $1.57 |
| `apply-cdc` | 51 | 2.114 | $0.93 |
| `partition-evolution` | 4 | 0.232 | $0.10 |
| `iceberg-maintenance` | 2 | 0.228 | $0.10 |
| `bronze-to-silver` | 2 | 0.195 | $0.09 |
| **Total** | 66 | **6.333** | **$2.79** |

These DPU-hours include the 2026-11-12 runs, so they are ahead of Cost Explorer's 5.22. The rate
is the Pricing API figure checked 2026-11-10 (open question 31).

- **Benchmarks are the largest item**, and one slow run dominates them. The 1M Silver load
  before the validation fix took 6,485 DPU-seconds (about $0.79). After the fix it took
  361 DPU-seconds (about $0.044), 18× cheaper for an identical result (ADR 0034). The 30-minute
  timeout run that preceded it cost 3,603 DPU-seconds.
- **The replay of the same 1M batch** took 273 DPU-seconds (about $0.033). The writer reads and
  validates the batch, then skips the write, because every table records the last batch it
  applied.
- **CDC apply** has a median of 113 DPU-seconds per run (about $0.014). Glue's 1-minute minimum
  and Spark start-up dominate it, not the data.
- **The scheduled daily maintenance** took 406 DPU-seconds on 2026-11-12 (about $0.05/day, about
  $1.50/month at list price). It is now the only recurring Glue cost.

### Athena: billed bytes come from the per-query minimum, not data volume

| Workgroup | Successful DML queries | Bytes processed |
|---|---|---|
| `procurelake-dev-engineering` | 5,558 | 808 MB |
| `procurelake-dev-tenant-alder` | 272 | 0.44 MB |
| `procurelake-dev-tenant-birch` | 268 | 0.41 MB |
| `procurelake-dev-analytics-admin` | 28 | 0.01 MB |

Cost Explorer billed 0.0293 TB (29.3 GB) for these 0.81 GB, because Athena bills at least 10 MB
per query. The cost driver is the **number of queries**: dbt models and tests, benchmark
repeats, and the test suites. It is not the size of the data. This is why partitioning saved
bytes but no money (ADR 0034). Consolidating dbt tests, or running fewer repeats, is the lever
until scans pass 10 MB.

### CDC stack (only while up)
Measured from the two sessions: DMS $0.040/h, RDS $0.018/h, the interface endpoint $0.011/h and
public IPv4 $0.005/h, about **$0.075 per hour** together. Six hours cost $0.45. `make cdc-down`
after every session is the control (ADR 0027).

## 3. Controls in place (plan §G "Cost controls")

| Control | Status |
|---|---|
| Budget and alerts before persistent infrastructure | Yes: foundation budget with email alerts (ADR 0019). Notifications, not caps |
| RDS and DMS only during CDC work and demos | Yes: disposable `dev/cdc` root, `make cdc-down` after every session; measured 6 h in total |
| Glue on demand, sensible frequency | Yes: on-demand jobs; one daily schedule (maintenance) |
| No NAT gateway, no always-on containers | Yes: no NAT anywhere (ADR 0027); dbt runs as a per-run Fargate task |
| S3 retention while keeping replay and audit data | Yes: lifecycle rules on the buckets; Bronze is versioned and never expires; Iceberg snapshot expiry runs daily |
| Track Athena bytes, Bedrock requests, Glue compute, ingestion | This review, from Cost Explorer, Glue run history and Athena metrics |
| `make destroy-dev` with safeguards | Yes: ADR 0038. Keeps state, lake data, checkpoints, review decisions, images and logs |

## 4. Recommendations (not applied)

1. **Glue 6.0** lists at $0.308 against $0.44 per DPU-hour (−30%), but changes Spark, Python and
   Iceberg versions (open question 31). It is the largest single saving available.
2. **Fewer Athena queries rather than smaller ones.** The 10 MB minimum makes query count the
   driver. Keep `bench-queries` repeats at 5 only for timing runs; use 1 for answer checks, as
   the replay check does.
3. **Run `make destroy-dev` when dev is idle for weeks.** It removes the daily maintenance
   schedule (about $1.50/month), the alarms and the dashboard. The KMS keys ($1/month each, list
   price) and lake storage stay.
