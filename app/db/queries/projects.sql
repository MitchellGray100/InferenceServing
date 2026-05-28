-- Project and membership queries.
--
-- Expected scope:
-- - create/list/get/delete projects
-- - create owner membership during project creation
-- - check project role for authorization decisions

-- name: create_project
-- Insert project metadata. The service generates slug and namespace before
-- calling this query so database rows already contain Kubernetes-safe names.
INSERT INTO projects (name, slug, k8s_namespace)
VALUES (%(name)s, %(slug)s, %(k8s_namespace)s)
RETURNING project_id, name, slug, k8s_namespace, created_at;

-- name: create_project_member
-- Add a user to a project with a specific role. Project creation uses this in
-- the same transaction to create the first owner membership.
INSERT INTO project_members (project_id, user_id, role)
VALUES (%(project_id)s, %(user_id)s, %(role)s)
RETURNING project_member_id, project_id, user_id, role, created_at;

-- name: list_projects_for_user
-- List all projects visible to a user by joining through membership. The role
-- is returned so the dashboard can display the user's permission level.
SELECT
  p.project_id,
  p.name,
  p.slug,
  p.k8s_namespace,
  p.created_at,
  pm.role
FROM projects p
JOIN project_members pm ON pm.project_id = p.project_id
WHERE pm.user_id = %(user_id)s
ORDER BY p.created_at DESC;

-- name: get_project_for_user
-- Fetch one project only if the user is a member. This supports 404-style
-- access hiding for projects outside the user's membership boundary.
SELECT
  p.project_id,
  p.name,
  p.slug,
  p.k8s_namespace,
  p.created_at,
  pm.role
FROM projects p
JOIN project_members pm ON pm.project_id = p.project_id
WHERE p.project_id = %(project_id)s
  AND pm.user_id = %(user_id)s;

-- name: get_project_member_role
-- Small authorization lookup used throughout services before project-scoped
-- reads or writes.
SELECT role
FROM project_members
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s;

-- name: list_project_members
-- Return project membership rows with user emails for dashboard/member views.
SELECT
  u.user_id,
  u.email,
  pm.role,
  pm.created_at
FROM project_members pm
JOIN users u ON u.user_id = pm.user_id
WHERE pm.project_id = %(project_id)s
ORDER BY pm.created_at ASC;

-- name: add_project_member_by_email
-- Add an existing user by email in one SQL statement. This query is retained
-- for service paths that want insert-by-select behavior.
INSERT INTO project_members (project_id, user_id, role)
SELECT %(project_id)s, u.user_id, %(role)s
FROM users u
WHERE u.email = %(email)s
RETURNING project_member_id, project_id, user_id, role, created_at;

-- name: update_project_member_role
-- Change a member's role after the service has verified owner permissions and
-- protected the last-owner invariant.
UPDATE project_members
SET role = %(role)s
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s
RETURNING project_member_id, project_id, user_id, role, created_at;

-- name: get_project_member
-- Fetch one member plus email. Used before role changes/removal so services
-- can validate membership and preserve response shape.
SELECT
  u.user_id,
  u.email,
  pm.role,
  pm.created_at
FROM project_members pm
JOIN users u ON u.user_id = pm.user_id
WHERE pm.project_id = %(project_id)s
  AND pm.user_id = %(user_id)s;

-- name: remove_project_member
-- Remove a user from a project after owner permission and last-owner checks.
DELETE FROM project_members
WHERE project_id = %(project_id)s
  AND user_id = %(user_id)s
RETURNING project_member_id;

-- name: count_project_owners
-- Count current owners so the service can prevent downgrading/removing the
-- final project owner.
SELECT COUNT(*) AS owner_count
FROM project_members
WHERE project_id = %(project_id)s
  AND role = 'owner';

-- name: delete_project
-- Delete a project after owner authorization. Foreign keys cascade most
-- product metadata tied to the project.
DELETE FROM projects
WHERE project_id = %(project_id)s
RETURNING project_id;
