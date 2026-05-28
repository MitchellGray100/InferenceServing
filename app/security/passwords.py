"""Password hashing and verification.

This module will wrap Argon2id or bcrypt so the rest of the app never handles
plaintext passwords beyond request validation.
"""
