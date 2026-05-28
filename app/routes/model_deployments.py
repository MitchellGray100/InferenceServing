"""Model deployment lifecycle routes.

These endpoints create deployment metadata and enqueue `deployment_jobs`. They
should not call Kubernetes directly from request handlers.
"""
