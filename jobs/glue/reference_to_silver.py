"""AWS Glue 5.1 entrypoint: apply pending reference-data batches (USAspending) to Silver Iceberg.

Thin by design (AGENTS.md section 4): all logic is in ``procurelake.lakehouse.silver.reference_job``
(ADR 0044). Do not call ``sys.exit``: Glue marks any ``SystemExit`` as a failed run.
"""

import sys

from procurelake.lakehouse.silver.reference_job import main

main(sys.argv[1:])
