"""Kubernetes manifest builders for vLLM deployments.

Manifest builders translate model_deployments rows and deployment_jobs payloads
into Namespace, PVC, Deployment, Service, HPA, and optional Secret resources.
"""
