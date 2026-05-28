"""OpenAI-compatible inference routes.

Inference routes use project API keys, resolve `request.body.model` as the
project-local deployment name, and proxy the request to the vLLM Service.
"""
