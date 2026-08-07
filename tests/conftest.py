"""Test configuration.

The one thing that must happen before anything else imports `emaild`.

`Settings.model_config.env_file` is resolved when the class is *defined*, so the
choice of env file is fixed at import time. Without this, a test run on the
operator's machine reads their real `.env` -- picking up live MXRoute
credentials, the live encryption key, and whatever role happens to be set there.
Tests would then pass or fail depending on the developer's local configuration,
which is the opposite of what a test is for.
"""

from __future__ import annotations

import os

# Must precede any `emaild` import. Do not move below the imports.
os.environ["EMAILD_ENV_FILE"] = "none"

# Anything the operator has exported into their shell would leak in the same
# way, so clear the role-scoped secrets explicitly.
for _leaky in (
    "EMAILD_MXROUTE_SERVER",
    "EMAILD_MXROUTE_USERNAME",
    "EMAILD_MXROUTE_API_KEY",
    "EMAILD_MAILBOX_ENCRYPTION_KEY",
    "EMAILD_ROLE",
    "EMAILD_ENV",
):
    os.environ.pop(_leaky, None)
