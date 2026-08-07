"""Versioned public API surface.

Deliberately Resend-shaped (build_plan.md): the stated use case is handing this
API to an AI assistant scaffolding a new product, and matching a contract every
model already knows means correct integration code on the first attempt.

The one deliberate divergence is the delivery-status vocabulary, which reflects
what we can actually prove rather than what sounds reassuring. That difference is
the reason the project exists.
"""
