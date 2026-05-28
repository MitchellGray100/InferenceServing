"""Kubernetes/Postgres deployment status reconciliation.

The reconciler periodically compares live Kubernetes state with
model_deployments rows and corrects stale product-level statuses.
"""
