"""emaild -- internal transactional email API."""

import os

__version__ = "0.10.0-rc.1"

# Build provenance (first_production_packaging §13, release_rules §2). Injected
# by the Dockerfile from build args; "unknown" outside a container build. An
# artifact must be traceable to the commit it came from.
GIT_COMMIT = os.environ.get("EMAILD_GIT_COMMIT", "unknown")
BUILD_TIME = os.environ.get("EMAILD_BUILD_TIME", "unknown")

# Schema versions this application build understands (release_rules §11).
# The app refuses to start outside this range rather than attempting permissive
# compatibility. Widen deliberately when a migration lands.
MIN_SCHEMA_VERSION = 1
MAX_SCHEMA_VERSION = 2
