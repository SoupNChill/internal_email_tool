"""Read-only operator dashboard.

Deliberately small: it answers questions, it does not perform actions. Every
mutation lives in the admin CLI, where it is deliberate and audited. A dashboard
that can revoke keys or un-suppress addresses is a dashboard whose compromise
matters much more than one that can only read.

Server-rendered with Jinja2 rather than assembled from f-strings, because
message subjects and recipient addresses are caller-controlled data going into
HTML. Autoescaping is precisely the thing not to hand-roll.
"""
