"""Server-rendered MiniTen web interface.

The dashboard uses the same service layer as the JSON API. Pages and form
actions intentionally match the CLI command groups so users can move between
web and terminal workflows without learning a second product model.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.security.tokens import decode_access_token
from app.services import (
    analytics_service,
    api_key_service,
    auth_service,
    inference_service,
    model_deployment_service,
    project_service,
    user_service,
)
from app.utils.errors import ApiError


bp = Blueprint("dashboard", __name__)
Handler = Callable[..., Any]


def current_dashboard_user_id() -> str | None:
    """Return the logged-in dashboard user ID, clearing stale tokens."""
    token = session.get("access_token")
    if not token:
        return None
    try:
        return str(decode_access_token(token)["sub"])
    except ApiError:
        session.clear()
        return None
    except (KeyError, TypeError):
        session.clear()
        return None


def require_dashboard_user(view: Handler) -> Handler:
    """Require a dashboard session before rendering protected pages."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_dashboard_user_id() is None:
            flash("Log in to continue.", "warning")
            return redirect(url_for("dashboard.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def user_id() -> str:
    """Return the current dashboard user ID for protected routes."""
    current = current_dashboard_user_id()
    if current is None:
        raise ApiError("unauthorized", "Log in to continue.", 401)
    return current


def run_form_action(success_message: str, fallback: str, action: Callable[[], Any]) -> Any:
    """Run a form action and redirect with a flash message."""
    try:
        action()
        flash(success_message, "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(fallback)


def optional_int(value: str | None) -> int | None:
    """Convert optional numeric form input into int or None."""
    if value is None or value == "":
        return None
    return int(value)


def required_int(value: str | None, field: str) -> int:
    """Convert required numeric form input into int or raise an API error."""
    if value is None or value == "":
        raise ApiError(
            type="validation_error",
            message=f"{field} is required.",
            status_code=400,
            details={"field": field},
        )
    try:
        return int(value)
    except ValueError as exc:
        raise ApiError(
            type="validation_error",
            message=f"{field} must be an integer.",
            status_code=400,
            details={"field": field},
        ) from exc


def bool_field(value: str | None) -> bool | None:
    """Convert optional boolean select values into bool or None."""
    if value == "":
        return None
    return value == "true"


def deployment_settings_from_form(*, include_identity: bool) -> dict[str, Any]:
    """Build model deployment settings from dashboard form fields."""
    data: dict[str, Any] = {}
    if include_identity:
        data["name"] = request.form.get("name", "")
        data["model_id"] = request.form.get("model_id", "")

    replicas = optional_int(request.form.get("replicas"))
    if replicas is not None:
        data["replicas"] = replicas

    resources = {
        "cpu_request": request.form.get("cpu_request") or None,
        "cpu_limit": request.form.get("cpu_limit") or None,
        "memory_request": request.form.get("memory_request") or None,
        "memory_limit": request.form.get("memory_limit") or None,
        "gpu_count": optional_int(request.form.get("gpu_count")),
    }
    resources = {key: value for key, value in resources.items() if value is not None}
    if resources:
        data["resources"] = resources

    vllm = {
        "dtype": request.form.get("dtype") or None,
        "max_model_len": optional_int(request.form.get("max_model_len")),
    }
    vllm = {key: value for key, value in vllm.items() if value is not None}
    if vllm:
        data["vllm"] = vllm

    autoscaling = {
        "enabled": bool_field(request.form.get("autoscaling_enabled")),
        "min_replicas": optional_int(request.form.get("min_replicas")),
        "max_replicas": optional_int(request.form.get("max_replicas")),
        "target_cpu_utilization": optional_int(
            request.form.get("target_cpu_utilization")
        ),
    }
    autoscaling = {
        key: value for key, value in autoscaling.items() if value is not None
    }
    if autoscaling:
        data["autoscaling"] = autoscaling

    return data


def idempotency_key(action: str) -> str:
    """Generate an idempotency key for dashboard control-plane forms."""
    return f"web-{action}-{uuid.uuid4()}"


@bp.get("/")
def index() -> Any:
    """Render the product entry page or redirect authenticated users."""
    if current_dashboard_user_id():
        return redirect(url_for("dashboard.projects"))
    return render_template("dashboard/index.html")


@bp.route("/register", methods=["GET", "POST"])
def register() -> Any:
    """Create a user account."""
    email = request.form.get("email", "")
    if request.method == "POST":
        try:
            user_service.create_user(email, request.form.get("password"))
            flash("Account created. Log in to continue.", "success")
            return redirect(url_for("dashboard.login"))
        except ApiError as exc:
            flash(exc.message, "error")
    return render_template("dashboard/auth.html", mode="register", email=email)


@bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Create a dashboard session."""
    email = request.form.get("email", "")
    if request.method == "POST":
        try:
            response = auth_service.login(
                email,
                request.form.get("password"),
            )
            session["access_token"] = response["access_token"]
            flash("Logged in.", "success")
            return redirect(request.args.get("next") or url_for("dashboard.projects"))
        except ApiError as exc:
            flash(exc.message, "error")
    return render_template("dashboard/auth.html", mode="login", email=email)


@bp.post("/logout")
def logout() -> Any:
    """Clear dashboard session state."""
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("dashboard.login"))


@bp.post("/account/delete")
@require_dashboard_user
def delete_account() -> Any:
    """Delete the current user account."""

    def action() -> None:
        user_service.delete_user(user_id())
        session.clear()

    return run_form_action("Account deleted.", url_for("dashboard.login"), action)


@bp.get("/account")
@require_dashboard_user
def account() -> Any:
    """Show account details and account deletion controls."""
    return render_template(
        "dashboard/account.html",
        current_user=user_service.get_user(user_id()),
    )


@bp.route("/projects", methods=["GET", "POST"])
@require_dashboard_user
def projects() -> Any:
    """List projects and create new projects."""
    if request.method == "POST":
        return run_form_action(
            "Project created.",
            url_for("dashboard.projects"),
            lambda: project_service.create_project(user_id(), request.form.get("name")),
        )

    response = project_service.list_projects(user_id())
    return render_template(
        "dashboard/projects.html",
        projects=response["projects"],
    )


@bp.get("/projects/<project_id>")
@require_dashboard_user
def project_detail(project_id: str) -> Any:
    """Show one project workspace."""
    current = user_id()
    project = project_service.get_project(current, project_id)
    models = model_deployment_service.list_model_deployments(current, project_id)
    members = project_service.list_project_members(current, project_id)
    api_keys = api_key_service.list_api_keys(current, project_id)
    analytics = analytics_service.get_project_overview(current, project_id)
    return render_template(
        "dashboard/project_detail.html",
        project=project,
        models=models["modelDeployments"],
        members=members["members"],
        api_keys=api_keys["api_keys"],
        analytics=analytics,
    )


@bp.post("/projects/<project_id>/delete")
@require_dashboard_user
def project_delete(project_id: str) -> Any:
    """Delete a project."""
    return run_form_action(
        "Project deleted.",
        url_for("dashboard.projects"),
        lambda: project_service.delete_project(user_id(), project_id),
    )


@bp.post("/projects/<project_id>/members")
@require_dashboard_user
def member_add(project_id: str) -> Any:
    """Add a project member."""
    return run_form_action(
        "Member added.",
        url_for("dashboard.project_detail", project_id=project_id),
        lambda: project_service.add_project_member(
            user_id(),
            project_id,
            request.form.get("email"),
            request.form.get("role"),
        ),
    )


@bp.post("/projects/<project_id>/members/<target_user_id>/update")
@require_dashboard_user
def member_update(project_id: str, target_user_id: str) -> Any:
    """Update a member role."""
    return run_form_action(
        "Member role updated.",
        url_for("dashboard.project_detail", project_id=project_id),
        lambda: project_service.update_project_member_role(
            user_id(),
            project_id,
            target_user_id,
            request.form.get("role"),
        ),
    )


@bp.post("/projects/<project_id>/members/<target_user_id>/remove")
@require_dashboard_user
def member_remove(project_id: str, target_user_id: str) -> Any:
    """Remove a project member."""
    return run_form_action(
        "Member removed.",
        url_for("dashboard.project_detail", project_id=project_id),
        lambda: project_service.remove_project_member(user_id(), project_id, target_user_id),
    )


@bp.post("/projects/<project_id>/api-keys")
@require_dashboard_user
def api_key_create(project_id: str) -> Any:
    """Create an API key and show the raw value once."""
    try:
        key = api_key_service.create_api_key(
            user_id(),
            project_id,
            request.form.get("name"),
        )
        flash("API key created. Copy it now; it will not be shown again.", "success")
        return render_template(
            "dashboard/api_key_created.html",
            project_id=project_id,
            api_key=key,
        )
    except ApiError as exc:
        flash(exc.message, "error")
        return redirect(url_for("dashboard.project_detail", project_id=project_id))


@bp.post("/projects/<project_id>/api-keys/<api_key_id>/revoke")
@require_dashboard_user
def api_key_revoke(project_id: str, api_key_id: str) -> Any:
    """Revoke an API key."""
    destination = url_for("dashboard.project_detail", project_id=project_id)
    try:
        api_key_service.revoke_api_key(user_id(), project_id, api_key_id)
        flash("API key revoked.", "success")
    except ApiError as exc:
        if exc.type == "api_key_not_found":
            flash("API key was already removed.", "warning")
        else:
            flash(exc.message, "error")
    return redirect(destination)


@bp.route("/projects/<project_id>/models/new", methods=["GET", "POST"])
@require_dashboard_user
def model_new(project_id: str) -> Any:
    """Create a model deployment."""
    if request.method == "POST":
        return run_form_action(
            "Model deploy job queued.",
            url_for("dashboard.project_detail", project_id=project_id),
            lambda: model_deployment_service.create_model_deployment(
                user_id(),
                project_id,
                deployment_settings_from_form(include_identity=True),
            ),
        )

    return render_template(
        "dashboard/model_form.html",
        project=project_service.get_project(user_id(), project_id),
        model=None,
    )


@bp.route("/projects/<project_id>/models/<model_id>", methods=["GET", "POST"])
@require_dashboard_user
def model_detail(project_id: str, model_id: str) -> Any:
    """Show and update one model deployment."""
    current = user_id()
    if request.method == "POST":
        return run_form_action(
            "Model update job queued.",
            url_for("dashboard.model_detail", project_id=project_id, model_id=model_id),
            lambda: model_deployment_service.update_model_deployment_settings(
                current,
                project_id,
                model_id,
                deployment_settings_from_form(include_identity=False),
            ),
        )

    model = model_deployment_service.get_model_deployment(current, project_id, model_id)
    jobs = model_deployment_service.list_model_deployment_jobs(
        current,
        project_id,
        model_id,
    )
    status = model_deployment_service.get_model_deployment_status(
        current,
        project_id,
        model_id,
    )
    return render_template(
        "dashboard/model_detail.html",
        project=project_service.get_project(current, project_id),
        model=model,
        jobs=jobs["deploymentJobs"],
        status=status,
    )


@bp.post("/projects/<project_id>/models/<model_id>/<command>")
@require_dashboard_user
def model_command(project_id: str, model_id: str, command: str) -> Any:
    """Run a model lifecycle command."""
    commands: dict[str, Callable[[], Any]] = {
        "start": lambda: model_deployment_service.start_model_deployment(
            user_id(),
            project_id,
            model_id,
        ),
        "retry": lambda: model_deployment_service.start_model_deployment(
            user_id(),
            project_id,
            model_id,
        ),
        "stop": lambda: model_deployment_service.stop_model_deployment(
            user_id(),
            project_id,
            model_id,
        ),
        "sync": lambda: model_deployment_service.sync_model_deployment_status(
            user_id(),
            project_id,
            model_id,
        ),
        "scale": lambda: model_deployment_service.scale_model_deployment(
            user_id(),
            project_id,
            model_id,
            required_int(request.form.get("replicas"), "replicas"),
        ),
        "delete": lambda: model_deployment_service.delete_model_deployment(
            user_id(),
            project_id,
            model_id,
        ),
    }
    if command not in commands:
        flash("Unknown model command.", "error")
        return redirect(url_for("dashboard.model_detail", project_id=project_id, model_id=model_id))
    if request.headers.get("X-Requested-With") == "fetch":
        try:
            commands[command]()
            return ("", 204)
        except ApiError as exc:
            return (exc.message, exc.status_code)
    destination = (
        url_for("dashboard.project_detail", project_id=project_id)
        if command == "delete"
        else url_for("dashboard.model_detail", project_id=project_id, model_id=model_id)
    )
    return run_form_action(
        f"Model {'retry' if command == 'retry' else command} job queued.",
        destination,
        commands[command],
    )


@bp.get("/projects/<project_id>/models/<model_name>/logs")
@require_dashboard_user
def model_logs(project_id: str, model_name: str) -> Any:
    """Show model logs by project-local model name."""
    current = user_id()
    logs = model_deployment_service.list_model_logs(
        current,
        project_id,
        model_name,
        tail=request.args.get("tail"),
    )
    return render_template(
        "dashboard/model_logs.html",
        project=project_service.get_project(current, project_id),
        model=logs["model"],
        project_id=project_id,
        model_name=model_name,
        logs=logs["logs"],
    )


@bp.route("/inference", methods=["GET", "POST"])
@require_dashboard_user
def inference() -> Any:
    """Send a chat completion through a project API key."""
    response = None
    form = {
        "api_key": request.form.get("api_key", ""),
        "model": request.form.get("model", ""),
        "prompt": request.form.get("prompt", ""),
        "max_tokens": request.form.get("max_tokens", "128"),
        "temperature": request.form.get("temperature", "0"),
    }
    if request.method == "POST":
        try:
            response, _status = inference_service.chat_completions(
                form["api_key"],
                {
                    "model": form["model"],
                    "messages": [
                        {
                            "role": "user",
                            "content": form["prompt"],
                        }
                    ],
                    "max_tokens": optional_int(form["max_tokens"]) or 128,
                    "temperature": float(form["temperature"] or 0),
                },
            )
            flash("Inference request completed.", "success")
        except ApiError as exc:
            flash(exc.message, "error")
    return render_template("dashboard/inference.html", response=response, form=form)


@bp.get("/projects/<project_id>/analytics/<model_name>")
@require_dashboard_user
def model_analytics(project_id: str, model_name: str) -> Any:
    """Show model analytics matching CLI analytics commands."""
    current = user_id()
    return render_template(
        "dashboard/model_analytics.html",
        project=project_service.get_project(current, project_id),
        model_name=model_name,
        metrics=analytics_service.get_model_metrics(current, project_id, model_name),
        requests=analytics_service.list_model_requests(
            current,
            project_id,
            model_name,
            limit=request.args.get("limit"),
            status_code=request.args.get("status_code"),
            since=request.args.get("since"),
        ),
        events=analytics_service.list_model_events(current, project_id, model_name),
    )
