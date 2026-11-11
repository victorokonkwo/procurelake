"""AWS Glue 5.1 entrypoint: Iceberg partition evolution on the bench Silver tables (ADR 0034).

Thin by design (AGENTS.md section 4): the logic is in ``procurelake.lakehouse.partitioning``.
Never call ``sys.exit``: Glue marks any ``SystemExit`` as a failed run (ADR 0025).
"""

import sys

from procurelake.lakehouse.partitioning_job import main

main(sys.argv[1:])
