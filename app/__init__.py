"""Flask application factory for MiniTen.

The app factory keeps construction of the Flask process in one place. Route
blueprints, database hooks, and dashboard setup will be registered here as the
implementation grows.
"""

from flask import Flask

from app.config import Config
from app.routes import (
    api_keys,
    analytics,
    auth,
    health,
    inference,
    model_deployments,
    project_members,
    projects,
    users,
)
from app.utils.errors import register_error_handlers
from app.utils.logging import configure_logging, register_request_logging


def create_app(config_class: type[Config] = Config) -> Flask:
    """Create and configure a Flask app instance."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    configure_logging(app.config.get("LOG_LEVEL", "INFO"))
    register_request_logging(app)
    register_error_handlers(app)
    app.register_blueprint(health.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(users.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(project_members.bp)
    app.register_blueprint(api_keys.bp)
    app.register_blueprint(model_deployments.bp)
    app.register_blueprint(analytics.bp)
    app.register_blueprint(inference.bp)

    return app
