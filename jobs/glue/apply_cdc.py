"""AWS Glue 5.1 entrypoint: apply new Bronze CDC files (AWS DMS) to Silver Iceberg tables.

Thin by design (AGENTS.md section 4): the logic is in ``procurelake.cdc.glue_job``. Never call
``sys.exit``: Glue marks any ``SystemExit`` as a failed run (ADR 0025). Failure is a raise.
"""

import sys

from procurelake.cdc.glue_job import main

main(sys.argv[1:])
