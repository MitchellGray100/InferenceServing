"""Deployment job worker entrypoint.

The worker consumes queued deployment_jobs, applies Kubernetes lifecycle
changes, records model_events, and marks jobs succeeded, retrying, or failed.
"""
