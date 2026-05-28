"""Database helpers.

The DB package owns connection pooling, migration execution, SQL loading, and
query files. Application services should use this package instead of embedding
SQL strings in route handlers.
"""
