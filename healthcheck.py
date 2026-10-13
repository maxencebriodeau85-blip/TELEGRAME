"""Worker health, based on a recent successfully completed cycle."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


def healthy(path, now=None):
    try:
        data = json.loads(Path(path).read_text())
        timestamp = datetime.fromisoformat(data['updated_at'])
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
        return 0 <= age <= 300 and data['status'] == 'cycle_completed'
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == '__main__':
    sys.exit(0 if healthy(Path(os.getenv('BOT_STATE_DIR', 'data')) / 'heartbeat.json') else 1)
