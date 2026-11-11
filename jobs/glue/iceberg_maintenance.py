"""AWS Glue 5.1 entrypoint: daily Iceberg maintenance of Silver and Gold through Athena (ADR 0033).

Thin by design (AGENTS.md section 4): the logic is in ``procurelake.lakehouse.maintenance``.
Never call ``sys.exit``: Glue marks any ``SystemExit`` as a failed run (ADR 0025). Failure is a
raise.
"""

import sys

from procurelake.lakehouse.maintenance_job import main

main(sys.argv[1:])
