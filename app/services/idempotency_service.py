"""Control-plane idempotency logic.

This service will store and replay control-plane responses for matching
Idempotency-Key headers and reject reused keys with different request bodies.
"""
