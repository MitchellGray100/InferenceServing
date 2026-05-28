"""Local Flask API process entrypoint.

Gunicorn should use `wsgi:app` in Linux/Docker environments. This module exists
for local development and direct `python -m app.main` execution.
"""

from __future__ import annotations

from app import create_app
from app.config import Config


app = create_app()


def main() -> None:
    """Run the Flask development server for local API testing."""
    # The built-in Flask server is convenient for local development only. The
    # Docker image and deployment examples use Gunicorn against `wsgi:app`.
    app.run(host=Config.API_HOST, port=Config.API_PORT, debug=Config.API_DEBUG)


if __name__ == "__main__":
    main()
