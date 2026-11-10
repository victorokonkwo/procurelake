# ADR 0015: Runtime versions (Glue 5.1 / Python 3.11, PostgreSQL 17)

- **Status:** Accepted
- **Date:** 2026-11-10

## Context
The shared Python library (`src/procurelake/`) will be packaged as a wheel and run inside AWS Glue jobs, so the repo's Python version must match the Glue runtime. The local PostgreSQL must also match the planned RDS source, which AWS DMS reads for CDC.

At the time of writing, AWS documentation listed:
- Glue 5.1: Spark 3.5.6, Python 3.11, Java 17. Glue 5.0: Spark 3.5.4, Python 3.11. Glue 6.0: Spark 4.1.1, Python 3.13.
- DMS supports RDS for PostgreSQL 17.x and 18.x as a source from DMS 3.5.4 onward.

Sources: [AWS Glue versions](https://docs.aws.amazon.com/glue/latest/dg/release-notes.html), [PostgreSQL as a DMS source](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.PostgreSQL.html).

## Decision
- **AWS Glue 5.1** for Spark jobs, with **Python 3.11** for the whole repo (`.python-version`, `requires-python = ">=3.11,<3.12"`).
- **PostgreSQL 17.** Locally we use the Docker image `postgres:17.11`. RDS (Week 5) uses PostgreSQL 17.x, and DMS must be 3.5.4 or later.

## Consequences
- Spark 3.5 has the most Iceberg, Glue, and dbt-athena reference material available.
- Glue 6.0 and Spark 4 are deferred. Upgrading later means changing Python to 3.13 across the repo.
- Before Week 3, check the Iceberg library version bundled with Glue 5.1 and the job parameters it requires (open question 3).
- Before Week 5, check the exact RDS 17 minor version and the DMS engine version available in `ca-central-1`.

## Alternatives considered
- **Glue 6.0 / Python 3.13:** the newest runtime, but less proven with Iceberg on Spark 4.
- **PostgreSQL 18:** the latest on RDS, but DMS support for it is newer.
- **PostgreSQL 16:** the most conservative choice, but it gives no benefit for this project.
