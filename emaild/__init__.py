"""emaild -- internal transactional email API."""

__version__ = "0.1.0"

# Schema versions this application build understands (release_rules §11).
# The app refuses to start outside this range rather than attempting permissive
# compatibility. Widen deliberately when a migration lands.
MIN_SCHEMA_VERSION = 1
MAX_SCHEMA_VERSION = 1
