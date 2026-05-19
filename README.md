# Inference Serving (Miniten)

<img width="1112" height="362" alt="image" src="https://github.com/user-attachments/assets/8e06f9a5-c1fb-4f04-98ef-d271c724a9b8" />
<img width="2211" height="1171" alt="image" src="https://github.com/user-attachments/assets/f6f7ab21-b39f-49b1-9acb-072912177f96" />


Miniten is an OCI-hosted, multi-user inference serving platform for deploying open-source Hugging Face LLMs as vLLM workers on Oracle Kubernetes Engine.

It provides a Baseten-inspired workflow for deploying named model services, managing their lifecycle, and calling them from application code through OpenAI-compatible HTTP APIs.

## Features

- Multi-user authentication
- Project-scoped API keys
- Named model deployments per project
- Hugging Face LLM deployment with vLLM
- Kubernetes Deployment and Service orchestration
- Readiness and health checks
- Start, stop, inspect, and delete model deployments
- HPA-based autoscaling with configurable min/max replicas
- Structured logging and deployment metadata tracking
- OpenAI-compatible `/v1/chat/completions` API

## Example Usage

Deploy a model:

```bash
miniten models deploy Qwen/Qwen2.5-0.5B-Instruct \
  --name qwen-small-prod \
  --cpu 2 \
  --memory 8Gi \
  --gpu 0
```

List deployed models:

```bash
miniten models list
```

Inspect a model:

```bash
miniten models inspect qwen-small-prod
```

Stop a model:

```bash
miniten models stop qwen-small-prod
```

Start a model:

```bash
miniten models start qwen-small-prod
```

Scale a model:

```bash
miniten models scale qwen-small-prod --replicas 3
```

Call a deployed model from Python:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.miniten.dev/v1",
    api_key="mt_live_xxx",
)

response = client.chat.completions.create(
    model="qwen-small-prod",
    messages=[
        {"role": "user", "content": "Explain Kubernetes in one sentence."}
    ],
)

print(response.choices[0].message.content)
```

## Architecture

```text
Internet
  |
  v
OCI Load Balancer
  |
  v
Oracle Kubernetes Engine
  |
  +-- MiniTen API
  +-- MiniTen Dashboard
  +-- MiniTen Gateway
  +-- Postgres
  +-- vLLM model workers
```

Each project maps to a Kubernetes namespace. Each deployed model creates a Kubernetes `Deployment`, `Service`, and optional `HorizontalPodAutoscaler`.

## Deployment Identity

Users interact with models by their project-local deployment name.

```yaml
name: qwen-small-prod
model: Qwen/Qwen2.5-0.5B-Instruct
engine: vllm

resources:
  cpu: "2"
  memory: "8Gi"
  gpu: 0

autoscaling:
  enabled: true
  min_replicas: 1
  max_replicas: 3
  target_cpu_utilization: 70
```

The `name` field is the API-facing identifier. The Hugging Face `model` ID is used internally by vLLM.

For example, users call the deployment by name:

```json
{
  "model": "qwen-small-prod",
  "messages": [
    {
      "role": "user",
      "content": "Explain Kubernetes in one sentence."
    }
  ]
}
```

This allows the same Hugging Face model to be deployed multiple times in one project under different names, such as:

```text
qwen-small-dev
qwen-small-prod
qwen-small-gpu
```

## Kubernetes Resources

For a deployment named `qwen-small-prod`, MiniTen creates resources such as:

```text
Namespace:  miniten-personal
Deployment: qwen-small-prod-v1
Service:    qwen-small-prod
HPA:        qwen-small-prod-v1
Secret:     qwen-small-prod-secrets
```

The gateway forwards inference traffic to the internal Kubernetes service:

```text
http://qwen-small-prod.miniten-personal.svc.cluster.local:8000/v1/chat/completions
```

## Autoscaling

MiniTen supports Kubernetes HPA-based autoscaling.

Example config:

```yaml
autoscaling:
  enabled: true
  min_replicas: 1
  max_replicas: 3
  target_cpu_utilization: 70
```

MiniTen translates this into a Kubernetes `HorizontalPodAutoscaler` for the model deployment.

## API Keys

Users create project-scoped API keys for inference access.

```text
mt_live_xxx
```

The API key determines the project. The `model` field in the request determines which named deployment inside that project receives the request.

## Tech Stack

- Frontend/dashboard: HTML, CSS, JavaScript<br>
- Backend/control plane: Node.js + Express<br>
- Database: Postgres + Prisma<br>
- Kubernetes integration: @kubernetes/client-node<br>
- Gateway forwarding: Node.js + undici/fetch<br>
- Auth: JWT + argon2/bcrypt<br>

