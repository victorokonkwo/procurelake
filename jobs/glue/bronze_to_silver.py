"""AWS Glue 5.1 entrypoint: apply a PostgreSQL snapshot batch from Bronze to Silver Iceberg.

Thin by design (AGENTS.md section 4): all logic is in ``procurelake.lakehouse.silver.job``,
installed from the wheel passed in ``--additional-python-modules``. Job arguments are defined
in ``infrastructure/live/dev/data_platform`` and parsed by ``job.parse_args``.

Do not call ``sys.exit``: Glue marks a run FAILED on any ``SystemExit``, even ``SystemExit: 0``
(observed on the first dev run, 2026-11-10). Failure is signalled by raising.
"""

import sys

from procurelake.lakehouse.silver.job import main

main(sys.argv[1:])
