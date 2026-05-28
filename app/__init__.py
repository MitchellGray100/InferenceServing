"""Flask application factory for MiniTen.

The app factory keeps construction of the Flask process in one place. Route
blueprints, database hooks, and dashboard setup will be registered here as the
implementation grows.
"""

from flask import Flask

from app.config import Config


def create_app(config_class: type[Config] = Config) -> Flask:
    """Create and configure a Flask app instance."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    return app
