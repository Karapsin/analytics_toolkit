"""Run under the existing external analytics-toolkit Python environment."""

import contextlib
import io
import json
import sys

from analytics_toolkit import sql


jobs = json.load(sys.stdin)
results = []
for job in jobs:
    try:
        # Toolkit diagnostics can include private backend details. Only the
        # JSON result crosses this subprocess boundary; failures expose a type.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = sql.read(job["db_key"], job["query"])
        results.append({"rows": json.loads(frame.to_json(orient="records", date_format="iso"))})
    except Exception as error:
        reason = getattr(error.__cause__, "reason", None)
        results.append({"error_type": type(error).__name__, "network_reason": type(reason).__name__ if reason else None})
json.dump(results, sys.stdout, ensure_ascii=False, allow_nan=False)
