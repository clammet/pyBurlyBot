#!/usr/bin/env python
"""Container healthcheck: a fresh heartbeat file means the reactor is alive.

Deliberately does NOT test IRC connectivity - the bot's reconnecting factory
self-heals from disconnects, and restarting the container for a netsplit
would only make things worse.
"""

import os
import sys
import time

path = os.environ.get("PYBB_HEARTBEAT_FILE", "state/.heartbeat")
max_age = float(os.environ.get("PYBB_HEARTBEAT_MAX_AGE", "120"))

try:
    age = time.time() - os.path.getmtime(path)
except OSError:
    sys.exit(1)
sys.exit(0 if age <= max_age else 1)
