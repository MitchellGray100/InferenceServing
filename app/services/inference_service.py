"""Inference request routing logic.

This service will validate project API keys, resolve model names to deployments,
build internal Kubernetes Service URLs, proxy requests to vLLM, and record
inference metadata.
"""
