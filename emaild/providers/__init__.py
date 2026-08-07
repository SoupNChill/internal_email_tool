"""Provider adapters.

MXRoute-specific behaviour lives behind this boundary so that core logic never
depends on SMTP or DirectAdmin details. Today there is one implementation; the
boundary exists because the stated exit path is moving to SES if volume ever
demands it, and that should be a configuration change rather than a rewrite.
"""
