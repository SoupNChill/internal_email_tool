"""Delivery adapters.

Provider-specific behaviour lives here so the worker loop never knows what SMTP
is. Today there is one real implementation plus a sink; the boundary exists
because the stated exit path is SES if volume ever demands it.
"""
