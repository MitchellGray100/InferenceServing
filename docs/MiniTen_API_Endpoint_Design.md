# MiniTen API Endpoint Reference

This document describes the MiniTen MVP API surface.

All public API endpoints are versioned under:

```text
/v1
```

## API Categories

MiniTen has seven API groups:

```text
Users API
Auth API
Projects API
Project Members API
Project API Keys API
Model Deployment API
Analytics API
Inference API
```

| API Group | Purpose |
|---|---|
| Users API | Create, read, and delete user accounts |
| Auth API | Login/logout and token creation |
| Projects API | Create, list, inspect, and delete projects |
| Project Members API | Manage users inside a project |
| Project API Keys API | Create/revoke project-scoped inference API keys |
| Model Deployment API | Deploy, inspect, update, start, stop, delete, and log models |
| Analytics API | View usage metrics, request history, and lifecycle events |
| Inference API | Call deployed models through OpenAI-compatible endpoints |

---

## Authentication Types

MiniTen has two different authentication modes.

### User auth token

Used for dashboard/control-plane operations.

Header:

```http
Authorization: Bearer <user_access_token>
```

Used for:

```text
Create project
Deploy model
Start/stop model
Create API key
View analytics
```

### Project API key

Used for inference requests from external applications.

Header:

```http
Authorization: Bearer <project_api_key>
```

Used for:

```text
POST /v1/chat/completions
GET /v1/models
```

The project API key determines the project. The `model` field in the request body determines which named deployment inside that project receives the request.

---

## Naming Rules

All model operations use the project-local deployment name.

Example:

```text
qwen-small-prod
```

The Hugging Face model ID is stored separately as metadata:

```text
Qwen/Qwen2.5-0.5B-Instruct
```

The OpenAI-compatible request uses the MiniTen deployment name:

```json
{
  "model": "qwen-small-prod",
  "messages": []
}
```

Recommended model name format:

```text
lowercase letters
numbers
hyphens
must start and end with an alphanumeric character
```

Good names:

```text
qwen-small
qwen-small-prod
tinyllama-dev
llama-3b-gpu
```

Bad names:

```text
Qwen Small
qwen_small
qwen/small
-prod
prod-
```

---

# 1. Users API

The Users API manages account records.

For the MVP, user creation is separate from login. Login is handled by the Auth API.

## 1.1 Create user

```http
POST /v1/users
```

### Purpose

Creates a new user account.

Used by:

```text
Signup page
Initial account creation flow
```

### Auth

No auth required.

### Request

```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

### Behavior

```text
1. Normalize email.
2. Validate email format.
3. Check email uniqueness.
4. Hash password.
5. Insert row into users.
6. Return created user without hashed_password.
```

### Response

```json
{
  "userID": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
  "email": "user@example.com",
  "created_at": "2026-05-17T12:00:00Z",
  "last_login_at": null
}
```

### Errors

```json
{
  "error": {
    "type": "email_already_exists",
    "message": "A user with this email already exists."
  }
}
```

### Tables used

```text
users
```

---

## 1.2 Get user

```http
GET /v1/users/me
```

Returns the currently authenticated user's account record.

The user is resolved from the auth token. The client does not need to provide a user ID.

### Auth

User auth token required.

```http
Authorization: Bearer <access_token>
```

### Behavior

```text
1. Read Authorization header.
2. Validate access token.
3. Extract user_id from token claims.
4. Fetch user by user_id.
5. Return user without hashed_password.
```

### Response

```json
{
  "userID": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
  "email": "user@example.com",
  "created_at": "2026-05-17T12:00:00Z",
  "last_login_at": "2026-05-17T12:30:00Z"
}
```

### Errors

```json
{
  "error": {
    "type": "unauthorized",
    "message": "Missing or invalid access token."
  }
}
```

### Tables Used

```text
users
```


---

## 1.3 Delete user

```http
DELETE /v1/users/me
```

### Purpose

Deletes or disables a user account.

Used by:

```text
Account deletion flow
```

### Auth

User auth token required.

### Permissions

For MVP:

```text
Users can only delete their own account. The user is resolved from the auth token.
```

### Behavior

Performs soft deletion of data.

### Response

```json
{
  "deleted": true
}
```

### Tables used

```text
users
project_members
```

---

# 2. Auth API

The Auth API handles login and logout.

## 2.1 Login

```http
POST /v1/auth/login
```

### Purpose

Authenticates a user and returns a user access token.

Used by:

```text
Login page
CLI login command
Dashboard session creation
```

### Auth

No auth required.

### Request

```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

### Behavior

```text
1. Normalize email.
2. Find user by email.
3. Verify password against hashed_password.
4. Update users.last_login_at.
5. Return an access token and user info.
```

### Response

```json
{
  "access_token": "jwt_or_session_token",
  "token_type": "bearer",
  "user": {
    "userID": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
    "email": "user@example.com",
    "created_at": "2026-05-17T12:00:00Z",
    "last_login_at": "2026-05-17T12:30:00Z"
  }
}
```

### Errors

Use the same error for invalid email and invalid password to avoid account enumeration.

```json
{
  "error": {
    "type": "invalid_credentials",
    "message": "Invalid email or password."
  }
}
```

### Tables used

```text
users
```

---

## 2.2 Logout

```http
POST /v1/auth/logout
```

### Purpose

Ends the current user session.

Used by:

```text
Dashboard logout
CLI logout
```

### Auth

User auth token required.

### Behavior

If using stateless JWTs, the client can simply discard the token. The endpoint can still exist for consistent UX.

### Response

```json
{
  "logged_out": true
}
```

---

# 3. Projects API

The Projects API manages projects.

A project is the main isolation boundary for members, API keys, model deployments, inference requests, and Kubernetes namespace.

Each project maps to exactly one Kubernetes namespace.

## 3.1 Create project

```http
POST /v1/projects
```

### Purpose

Creates a new project and makes the current user the project owner.

Used by:

```text
New project page
Initial onboarding flow
```

### Auth

User auth token required.

### Request

```json
{
  "name": "Personal Models"
}
```

### Behavior

```text
1. Require authenticated user.
2. Generate slug from name.
3. Ensure slug is unique.
4. Generate k8s_namespace.
5. Insert row into projects.
6. Insert row into project_members with role = owner.
7. Return project.
```

### Response

```json
{
  "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
  "name": "Personal Models",
  "slug": "personal-models",
  "k8s_namespace": "miniten-personal-models",
  "created_at": "2026-05-17T12:00:00Z",
  "role": "owner"
}
```

### Tables used

```text
projects
project_members
```

---

## 3.2 List projects

```http
GET /v1/projects
```

### Purpose

Lists projects the authenticated user belongs to.

Used by:

```text
Project switcher
Dashboard home page
CLI project list command
```

### Auth

User auth token required.

### Behavior

```text
1. Require authenticated user.
2. Find projects where user is in project_members.
3. Return project list with user's role.
```

### Response

```json
{
  "projects": [
    {
      "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
      "name": "Personal Models",
      "slug": "personal-models",
      "k8s_namespace": "miniten-personal-models",
      "role": "owner",
      "created_at": "2026-05-17T12:00:00Z"
    }
  ]
}
```

### Tables used

```text
projects
project_members
```

---

## 3.3 Get project

```http
GET /v1/projects/{projectID}
```

### Purpose

Returns one project.

Used by:

```text
Project settings page
Dashboard project context loading
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Behavior

```text
1. Require authenticated user.
2. Verify user is a project member.
3. Return project metadata and user's role.
```

### Response

```json
{
  "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
  "name": "Personal Models",
  "slug": "personal-models",
  "k8s_namespace": "miniten-personal-models",
  "role": "owner",
  "created_at": "2026-05-17T12:00:00Z"
}
```

### Tables used

```text
projects
project_members
```

---

## 3.4 Delete project

```http
DELETE /v1/projects/{projectID}
```

### Purpose

Deletes a project and its associated resources.

Used by:

```text
Project settings danger zone
```

### Auth

User auth token required.

### Permissions

```text
owner
```

### Behavior

```text
1. Verify current user is project owner.
2. Delete Kubernetes namespace/resources for the project.
3. Delete project row or mark project as deleted.
4. Cascade delete memberships, model deployments, API keys, requests, and events.
```

### Response

```json
{
  "deleted": true
}
```

### Tables used

```text
projects
project_members
model_deployments
api_keys
inference_requests
model_events
```

### Kubernetes actions

```text
Delete project Kubernetes namespace
```

---

# 4. Project Members API

The Project Members API manages project membership.

Users must already have accounts before they can be added to a project.


## 4.1 List project members

```http
GET /v1/projects/{projectID}/members
```

### Purpose

Lists users who belong to a project.

Used by:

```text
Project members page
Project settings page
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Response

```json
{
  "members": [
    {
      "userID": "9d41b65e-1d5a-4f24-a4c6-98f4df0c2c5e",
      "email": "owner@example.com",
      "role": "owner",
      "created_at": "2026-05-17T12:00:00Z"
    },
    {
      "userID": "5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
      "email": "member@example.com",
      "role": "member",
      "created_at": "2026-05-17T12:10:00Z"
    }
  ]
}
```

### Tables used

```text
users
project_members
```

---

## 4.2 Add project member

```http
POST /v1/projects/{projectID}/members
```

### Purpose

Adds an existing user to a project.

Used by:

```text
Project members page
Add collaborator flow
```

### Auth

User auth token required.

### Permissions

Recommended MVP:

```text
owner
```

### Request

```json
{
  "email": "member@example.com",
  "role": "member"
}
```

### Behavior

```text
1. Verify current user is project owner.
2. Find target user by email.
3. Insert project_members row.
4. Return new member.
```

### Response

```json
{
  "userID": "5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
  "email": "member@example.com",
  "role": "member",
  "created_at": "2026-05-17T12:10:00Z"
}
```

### Errors

```json
{
  "error": {
    "type": "user_not_found",
    "message": "No user exists with that email."
  }
}
```

### Tables used

```text
users
project_members
```

---

## 4.3 Update project member role

```http
PATCH /v1/projects/{projectID}/members/{userID}
```

### Purpose

Updates a project member's role.

Used by:

```text
Project members page
Role management flow
```

### Auth

User auth token required.

### Permissions

```text
owner
```

### Request

```json
{
  "role": "viewer"
}
```

### Behavior

```text
1. Verify current user is project owner.
2. Verify role is owner, member, or viewer.
3. Update project_members.role.
4. Prevent removing/downgrading the last owner.
```

### Response

```json
{
  "userID": "5bcb59e9-86e8-4ac9-b5c6-66f332697a0c",
  "email": "member@example.com",
  "role": "viewer",
  "created_at": "2026-05-17T12:10:00Z"
}
```

### Tables used

```text
users
project_members
```

---

## 4.4 Remove project member

```http
DELETE /v1/projects/{projectID}/members/{userID}
```

### Purpose

Removes a user from a project.

Used by:

```text
Project members page
Remove collaborator flow
```

### Auth

User auth token required.

### Permissions

```text
owner
```

### Behavior

```text
1. Verify current user is project owner.
2. Delete project_members row.
3. Prevent removing the last owner.
```

### Response

```json
{
  "removed": true
}
```

### Tables used

```text
project_members
```

---

# 5. Project API Keys API

Project API keys are used by external applications to call deployed models.

API keys are project-scoped. They are not account-scoped.

The raw API key is only returned once on creation.

## 5.1 Create API key

```http
POST /v1/projects/{projectID}/api-keys
```

### Purpose

Creates a project-scoped API key.

Used by:

```text
API keys dashboard page
CLI create key command
External application setup
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Request

```json
{
  "name": "local-dev"
}
```

### Behavior

```text
1. Verify user is project owner or member.
2. Generate raw API key.
3. Derive key_prefix from raw key.
4. Hash raw key with server-secret HMAC.
5. Store key_prefix and key_hash.
6. Return raw API key once.
```

### Response

```json
{
  "apiKeyID": "1c45e99d-98f7-464a-8b4e-f4c1d8fe6d37",
  "projectID": "9f943ed3-881e-4f49-b9df-f19eb151c8c1",
  "name": "local-dev",
  "key_prefix": "mt_live_x7k2",
  "api_key": "mt_live_x7k2_8sdf9as7df0qwer...",
  "created_at": "2026-05-17T12:00:00Z",
  "last_used_at": null,
  "revoked_at": null
}
```

### Important Security Note

The `api_key` field is only returned during creation. It should never be retrievable again. MiniTen stores only `key_prefix` and a server-secret HMAC of the full raw key.

### Tables used

```text
api_keys
project_members
```

---

## 5.2 List API keys

```http
GET /v1/projects/{projectID}/api-keys
```

### Purpose

Lists API key metadata for a project.

Used by:

```text
API keys dashboard page
CLI list keys command
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Response

```json
{
  "api_keys": [
    {
      "apiKeyID": "1c45e99d-98f7-464a-8b4e-f4c1d8fe6d37",
      "name": "local-dev",
      "key_prefix": "mt_live_x7k2",
      "created_at": "2026-05-17T12:00:00Z",
      "last_used_at": "2026-05-17T12:30:00Z",
      "revoked_at": null
    }
  ]
}
```

### Important Security Note

Do not return:

```text
raw API key
key_hash
```

### Tables used

```text
api_keys
project_members
```

---

## 5.3 Revoke API key

```http
DELETE /v1/projects/{projectID}/api-keys/{apiKeyID}
```

### Purpose

Revokes a project API key.

Used by:

```text
API keys dashboard page
CLI revoke key command
Security incident response
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Behavior

```text
1. Verify user is project owner or member.
2. Verify API key belongs to project.
3. Set revoked_at = now().
4. Preserve row for historical request logs.
```

### Response

```json
{
  "revoked": true
}
```

### Tables used

```text
api_keys
```

---

# 6. Model Deployment API

The Model Deployment API manages model infrastructure.

These endpoints are for creating, updating, starting, stopping, deleting, and inspecting model deployments.

They do not send inference prompts to models. Inference happens through `/v1/chat/completions`.

## 6.1 Deploy model

```http
POST /v1/projects/{projectID}/models
```

### Purpose

Creates a new named model deployment in a project.

Used by:

```text
Deploy model page
CLI deploy command
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Request

```json
{
  "name": "qwen-small-prod",
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "resources": {
    "cpu_request": "2",
    "cpu_limit": "4",
    "memory_request": "8Gi",
    "memory_limit": "16Gi",
    "gpu_count": 0
  },
  "vllm": {
    "image": "vllm/vllm-openai:latest",
    "dtype": "auto",
    "max_model_len": 4096
  },
  "autoscaling": {
    "enabled": true,
    "min_replicas": 1,
    "max_replicas": 3,
    "target_cpu_utilization": 70
  }
}
```

### Autoscaling Storage Rule

Autoscaling is supported in the MVP.

MiniTen uses a shared PVC-backed Hugging Face cache by default. When autoscaling creates more than one replica for a deployment, the configured storage class must support mounting that cache across replicas with a compatible access mode such as `ReadWriteMany`.

If the local or cloud cluster does not support a compatible shared volume mode, the deployment may still run with one replica, but multi-replica autoscaling with a shared cache is not guaranteed.

### Behavior

```text
1. Verify user is project owner or member.
2. Validate deployment name.
3. Ensure name is unique within project.
4. Insert model_deployments row with status = deploying.
5. Insert deployment_jobs row with job_type = deploy_model.
6. Insert model_events row: deploy_requested.
7. Return queued/deploying deployment object.
```

### Response

```json
{
  "modelDeploymentID": "7a16ad8b-3d7d-4dd3-9a63-c4e3bbf29c18",
  "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
  "name": "qwen-small-prod",
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "status": "deploying",
  "k8s_deployment_name": "qwen-small-prod-v1",
  "k8s_service_name": "qwen-small-prod",
  "k8s_hpa_name": "qwen-small-prod-v1",
  "replicas": 1,
  "autoscaling": {
    "enabled": true,
    "min_replicas": 1,
    "max_replicas": 3,
    "target_cpu_utilization": 70
  },
  "created_at": "2026-05-17T12:00:00Z"
}
```

### Tables used

```text
projects
project_members
model_deployments
deployment_jobs
model_events
```

### Kubernetes actions

```text
None directly in the request handler.
The Deployment Worker creates Namespace, PVC, Deployment, Service, HPA, and Secret resources from the deployment_jobs row.
```

---

## 6.2 List models

```http
GET /v1/projects/{projectID}/models
```

### Purpose

Lists model deployments in a project.

Used by:

```text
Models dashboard page
CLI list models command
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Response

```json
{
  "models": [
    {
      "modelDeploymentID": "7a16ad8b-3d7d-4dd3-9a63-c4e3bbf29c18",
      "name": "qwen-small-prod",
      "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
      "status": "running",
      "replicas": 2,
      "autoscaling_enabled": true,
      "created_at": "2026-05-17T12:00:00Z"
    }
  ]
}
```

### Tables used

```text
model_deployments
project_members
```

---

## 6.3 Get model

```http
GET /v1/projects/{projectID}/models/{modelName}
```

### Purpose

Returns details for one named model deployment.

Used by:

```text
Model detail page
CLI inspect model command
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Response

```json
{
  "modelDeploymentID": "7a16ad8b-3d7d-4dd3-9a63-c4e3bbf29c18",
  "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
  "name": "qwen-small-prod",
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "status": "running",
  "k8s_deployment_name": "qwen-small-prod-v1",
  "k8s_service_name": "qwen-small-prod",
  "k8s_hpa_name": "qwen-small-prod-v1",
  "replicas": 2,
  "resources": {
    "cpu_request": "2",
    "cpu_limit": "4",
    "memory_request": "8Gi",
    "memory_limit": "16Gi",
    "gpu_count": 0
  },
  "vllm": {
    "image": "vllm/vllm-openai:latest",
    "dtype": "auto",
    "max_model_len": 4096
  },
  "autoscaling": {
    "enabled": true,
    "min_replicas": 1,
    "max_replicas": 3,
    "target_cpu_utilization": 70
  },
  "created_at": "2026-05-17T12:00:00Z"
}
```

### Tables used

```text
model_deployments
project_members
```

---

## 6.4 Update model

```http
PATCH /v1/projects/{projectID}/models/{modelName}
```

### Purpose

Updates an existing model deployment's serving configuration.

Used by:

```text
Model settings page
Autoscaling settings page
Resource configuration page
CLI update model command
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Request

```json
{
  "replicas": 3,
  "resources": {
    "cpu_request": "4",
    "cpu_limit": "8",
    "memory_request": "16Gi",
    "memory_limit": "32Gi",
    "gpu_count": 1
  },
  "vllm": {
    "dtype": "float16",
    "max_model_len": 8192
  },
  "autoscaling": {
    "enabled": true,
    "min_replicas": 1,
    "max_replicas": 5,
    "target_cpu_utilization": 70
  }
}
```

### Behavior

```text
1. Verify user is project owner or member.
2. Find model by projectID + modelName.
3. Validate requested changes.
4. Update desired configuration in model_deployments.
5. Insert deployment_jobs row with job_type = scale_model or sync_status, depending on the change.
6. Insert model_events row such as model_scaled when the worker applies the change.
7. Return queued/updated model.
```

### Important Rules

Easy to patch:

```text
replicas
cpu_request
cpu_limit
memory_request
memory_limit
autoscaling_enabled
min_replicas
max_replicas
target_cpu_utilization
```

May require pod restart:

```text
gpu_count
vllm_dtype
vllm_max_model_len
vllm_image
```

Should not be editable in-place:

```text
model_id
```

If the user wants a different Hugging Face model, they should create a new named deployment.

If autoscaling is enabled, direct `replicas` updates should be rejected or ignored. Users should update `min_replicas` and `max_replicas` instead.

### Response

```json
{
  "name": "qwen-small-prod",
  "status": "running",
  "replicas": 3,
  "autoscaling": {
    "enabled": false,
    "min_replicas": null,
    "max_replicas": null,
    "target_cpu_utilization": null
  }
}
```

### Tables used

```text
model_deployments
deployment_jobs
model_events
```

### Kubernetes actions

```text
None directly in the request handler.
The Deployment Worker patches Deployment and HPA resources, and restarts pods if required.
```

---

## 6.5 Start model

```http
POST /v1/projects/{projectID}/models/{modelName}/start
```

### Purpose

Starts a stopped model.

Used by:

```text
Model dashboard start button
CLI start model command
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Request

Optional:

```json
{
  "replicas": 1
}
```

### Behavior

```text
1. Verify user is project owner or member.
2. Find model by projectID + modelName.
3. Insert deployment_jobs row with job_type = start_model.
4. Store requested replicas or autoscaling restore intent in the job payload.
5. Update status to loading.
6. Insert model_events row: model_started.
7. Return queued/loading model.
```

### Response

```json
{
  "name": "qwen-small-prod",
  "status": "loading",
  "replicas": 1
}
```

### Tables used

```text
model_deployments
deployment_jobs
model_events
```

### Kubernetes actions

```text
None directly in the request handler.
The Deployment Worker scales the Deployment up and restores HPA settings if needed.
```

---

## 6.6 Stop model

```http
POST /v1/projects/{projectID}/models/{modelName}/stop
```

### Purpose

Stops a running model.

Used by:

```text
Model dashboard stop button
CLI stop model command
Cost control
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Behavior

```text
1. Verify user is project owner or member.
2. Find model by projectID + modelName.
3. Insert deployment_jobs row with job_type = stop_model.
4. Store HPA suspension or min_replicas = 0 intent in the job payload when autoscaling is enabled.
5. Update status = stopped or stopping-equivalent queued state.
6. Insert model_events row: model_stopped.
7. Return queued/stopped model.
```

### Response

```json
{
  "name": "qwen-small-prod",
  "status": "stopped",
  "replicas": 0
}
```

### Tables used

```text
model_deployments
deployment_jobs
model_events
```

### Important Rule

If autoscaling is enabled, stop must handle the HPA first. Otherwise HPA may scale the deployment back up.

### Kubernetes actions

```text
None directly in the request handler.
The Deployment Worker handles HPA first, then scales the Deployment to zero.
```

---

## 6.7 Delete model

```http
DELETE /v1/projects/{projectID}/models/{modelName}
```

### Purpose

Deletes a model deployment and removes Kubernetes resources.

Used by:

```text
Model dashboard delete button
CLI delete model command
```

### Auth

User auth token required.

### Permissions

```text
owner, member
```

### Behavior

```text
1. Verify user is project owner or member.
2. Find model by projectID + modelName.
3. Set status = deleting.
4. Insert deployment_jobs row with job_type = delete_model.
5. Insert model_events row: model_deleted when the worker finishes deletion.
6. Return queued/deleting response.
```

### Response

```json
{
  "deleted": true
}
```

### Tables used

```text
model_deployments
deployment_jobs
model_events
```

### Kubernetes actions

```text
None directly in the request handler.
The Deployment Worker deletes HPA, Service, Deployment, Secret, and other deployment-owned resources as needed.
```

---

## 6.8 Get model logs

```http
GET /v1/projects/{projectID}/models/{modelName}/logs
```

### Purpose

Fetches recent logs from the Kubernetes pods running the vLLM worker.

Used by:

```text
Model logs page
Debugging failed deployments
CLI logs command
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Query Parameters

```text
tail optional, default 200
```

Example:

```http
GET /v1/projects/{projectID}/models/qwen-small-prod/logs?tail=200
```

### Response

```json
{
  "model": "qwen-small-prod",
  "logs": [
    {
      "pod": "qwen-small-prod-v1-abc123",
      "line": "INFO vLLM server started",
      "timestamp": "2026-05-17T12:00:00Z"
    }
  ]
}
```

### Kubernetes actions

```text
Read pod logs
```

---

# 7. Analytics API

The Analytics API is for usage, metrics, request history, and model events.

These endpoints do not modify infrastructure.

## 7.1 Project analytics overview

```http
GET /v1/projects/{projectID}/analytics/overview
```

### Purpose

Returns a project-level usage summary across all models.

Used by:

```text
Dashboard overview page
Project analytics page
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Response

```json
{
  "projectID": "a2fc41b7-862e-4060-b466-2376f29227bb",
  "summary": {
    "total_models": 3,
    "running_models": 2,
    "stopped_models": 1,
    "total_requests": 1240,
    "error_count": 12,
    "average_latency_ms": 842,
    "last_request_at": "2026-05-17T12:30:00Z"
  },
  "models": [
    {
      "name": "qwen-small-prod",
      "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
      "status": "running",
      "request_count": 800,
      "error_count": 5,
      "average_latency_ms": 790,
      "last_request_at": "2026-05-17T12:30:00Z"
    }
  ]
}
```

### Tables used

```text
model_deployments
inference_requests
```

---

## 7.2 Model metrics

```http
GET /v1/projects/{projectID}/analytics/models/{modelName}/metrics
```

### Purpose

Returns aggregate metrics for one named model deployment.

Used by:

```text
Model analytics page
Model detail page
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Query Parameters

```text
since optional ISO timestamp
```

Example:

```http
GET /v1/projects/{projectID}/analytics/models/qwen-small-prod/metrics?since=2026-05-17T00:00:00Z
```

### Response

```json
{
  "model": {
    "name": "qwen-small-prod",
    "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
    "status": "running"
  },
  "metrics": {
    "request_count": 800,
    "success_count": 795,
    "error_count": 5,
    "average_latency_ms": 790,
    "p95_latency_ms": 1320,
    "last_request_at": "2026-05-17T12:30:00Z"
  }
}
```

### Tables used

```text
model_deployments
inference_requests
```

---

## 7.3 Model request history

```http
GET /v1/projects/{projectID}/analytics/models/{modelName}/requests
```

### Purpose

Returns recent inference requests for one model.

Used by:

```text
Model request history page
Debugging request failures
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Query Parameters

```text
limit optional, default 100
status_code optional
since optional ISO timestamp
```

Example:

```http
GET /v1/projects/{projectID}/analytics/models/qwen-small-prod/requests?limit=50
```

### Response

```json
{
  "requests": [
    {
      "inferenceRequestID": "392545ef-5fe2-4938-9b95-7a8538ddff8d",
      "status_code": 200,
      "latency_ms": 842,
      "error_type": null,
      "request_path": "/v1/chat/completions",
      "method": "POST",
      "streamed": false,
      "created_at": "2026-05-17T12:30:00Z"
    },
    {
      "inferenceRequestID": "cc089913-453b-4aca-a26b-5043166b20f1",
      "status_code": 409,
      "latency_ms": 12,
      "error_type": "model_stopped",
      "request_path": "/v1/chat/completions",
      "method": "POST",
      "streamed": false,
      "created_at": "2026-05-17T12:25:00Z"
    }
  ]
}
```

### Important Privacy Note

Do not return prompts or model responses in the MVP.

### Tables used

```text
model_deployments
inference_requests
```

---

## 7.4 Model events

```http
GET /v1/projects/{projectID}/analytics/models/{modelName}/events
```

### Purpose

Returns lifecycle event history for one model deployment.

Used by:

```text
Model events page
Deployment debugging
Audit-like deployment history
```

### Auth

User auth token required.

### Permissions

```text
owner, member, viewer
```

### Response

```json
{
  "events": [
    {
      "modelEventID": "5084fc13-01bd-48c4-b5b7-df9044a5d2c9",
      "event_type": "deploy_requested",
      "message": "Deployment requested for qwen-small-prod",
      "metadata": {
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct"
      },
      "created_at": "2026-05-17T12:00:00Z"
    },
    {
      "modelEventID": "ce8ac720-34ba-42d5-8e30-77b064f07eed",
      "event_type": "k8s_deployment_created",
      "message": "Created Kubernetes Deployment qwen-small-prod-v1",
      "metadata": {
        "deployment": "qwen-small-prod-v1"
      },
      "created_at": "2026-05-17T12:01:00Z"
    }
  ]
}
```

### Tables used

```text
model_deployments
model_events
```

---

# 8. Inference API

The Inference API is the public OpenAI-compatible API used by external applications.

These endpoints use project API keys, not dashboard user tokens.

The project comes from the API key. The model comes from the request body.

## 8.1 Chat completions

```http
POST /v1/chat/completions
```

### Purpose

Sends chat messages to a deployed model using an OpenAI-compatible request format.

Used by:

```text
External applications
SDK integrations
User codebases
CLI chat command
```

### Auth

Project API key required.

Header:

```http
Authorization: Bearer mt_live_xxx
```

### Request

```json
{
  "model": "qwen-small-prod",
  "messages": [
    {
      "role": "user",
      "content": "Explain Kubernetes in one sentence."
    }
  ],
  "max_tokens": 128
}
```

### Behavior

```text
1. Extract project API key.
2. Hash key and find api_keys row.
3. Verify key is active and not revoked.
4. Resolve project_id from API key.
5. Read request.body.model.
6. Treat request.body.model as the project-local deployment name.
7. Find model_deployments row where project_id = key.project_id and name = body.model.
8. Verify model status is running.
9. Build internal Kubernetes Service URL.
10. Forward request to vLLM.
11. Return vLLM response.
12. Insert inference_requests row.
13. Update api_keys.last_used_at.
```

### Routing Example

```text
API key project: personal
Request model: qwen-small-prod
Kubernetes namespace: miniten-personal
Kubernetes service: qwen-small-prod

Forward to:
http://qwen-small-prod.miniten-personal.svc.cluster.local:8000/v1/chat/completions
```

### Response

The response should mirror the vLLM/OpenAI-compatible response.

Example shape:

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1779048000,
  "model": "qwen-small-prod",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Kubernetes is a platform for running and managing containerized applications across a cluster of machines."
      },
      "finish_reason": "stop"
    }
  ]
}
```

### Errors

If model is stopped:

```json
{
  "error": {
    "type": "model_stopped",
    "message": "Model qwen-small-prod is stopped. Start it from the dashboard before sending inference requests."
  }
}
```

If model does not exist in project:

```json
{
  "error": {
    "type": "model_not_found",
    "message": "No model named qwen-small-prod exists in this project."
  }
}
```

### Tables used

```text
api_keys
model_deployments
inference_requests
```

### Kubernetes actions

```text
Forward HTTP request to model Service
```

---

## 8.2 List available inference models

```http
GET /v1/models
```

### Purpose

Lists model deployments available to the project API key.

Used by:

```text
External applications
OpenAI-compatible SDK model listing
CLI model discovery
```

### Auth

Project API key required.

Header:

```http
Authorization: Bearer mt_live_xxx
```

### Behavior

```text
1. Validate project API key.
2. Resolve project_id.
3. Return non-deleted model deployments in the project.
```

### Response

OpenAI-compatible shape:

```json
{
  "object": "list",
  "data": [
    {
      "id": "qwen-small-prod",
      "object": "model",
      "owned_by": "project"
    },
    {
      "id": "tinyllama-dev",
      "object": "model",
      "owned_by": "project"
    }
  ]
}
```

### Tables used

```text
api_keys
model_deployments
```

---

# Error Response Format

Use a consistent error shape across APIs:

```json
{
  "error": {
    "type": "error_type",
    "message": "Human-readable explanation."
  }
}
```

Common error types:

```text
invalid_credentials
unauthorized
forbidden
not_found
validation_error
email_already_exists
user_not_found
project_not_found
model_not_found
model_stopped
api_key_revoked
duplicate_model_name
autoscaling_enabled
kubernetes_error
```

---

# Permission Summary

| Resource | Owner | Member | Viewer |
|---|---:|---:|---:|
| View project | Yes | Yes | Yes |
| Delete project | Yes | No | No |
| Manage members | Yes | No | No |
| Create API key | Yes | Yes | No |
| List API keys | Yes | Yes | Yes |
| Revoke API key | Yes | Yes | No |
| Deploy model | Yes | Yes | No |
| Update model | Yes | Yes | No |
| Start/stop model | Yes | Yes | No |
| Delete model | Yes | Yes | No |
| View logs | Yes | Yes | Yes |
| View analytics | Yes | Yes | Yes |
| Call inference API with project key | Yes | Yes | Yes, if they have access to the key |

---

# Final Endpoint List

## Users

```http
POST   /v1/users
GET    /v1/users/me
DELETE /v1/users/me
```

## Auth

```http
POST /v1/auth/login
POST /v1/auth/logout
```

## Projects

```http
POST   /v1/projects
GET    /v1/projects
GET    /v1/projects/{projectID}
DELETE /v1/projects/{projectID}
```

## Project Members

```http
GET    /v1/projects/{projectID}/members
POST   /v1/projects/{projectID}/members
PATCH  /v1/projects/{projectID}/members/{userID}
DELETE /v1/projects/{projectID}/members/{userID}
```

## Project API Keys

```http
POST   /v1/projects/{projectID}/api-keys
GET    /v1/projects/{projectID}/api-keys
DELETE /v1/projects/{projectID}/api-keys/{apiKeyID}
```

## Model Deployments

```http
POST   /v1/projects/{projectID}/models
GET    /v1/projects/{projectID}/models
GET    /v1/projects/{projectID}/models/{modelName}
PATCH  /v1/projects/{projectID}/models/{modelName}
POST   /v1/projects/{projectID}/models/{modelName}/start
POST   /v1/projects/{projectID}/models/{modelName}/stop
DELETE /v1/projects/{projectID}/models/{modelName}
GET    /v1/projects/{projectID}/models/{modelName}/logs
```

## Analytics

```http
GET /v1/projects/{projectID}/analytics/overview
GET /v1/projects/{projectID}/analytics/models/{modelName}/metrics
GET /v1/projects/{projectID}/analytics/models/{modelName}/requests
GET /v1/projects/{projectID}/analytics/models/{modelName}/events
```

## Inference

```http
POST /v1/chat/completions
GET  /v1/models
```
