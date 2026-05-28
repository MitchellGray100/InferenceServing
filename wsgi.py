"""WSGI entrypoint for Gunicorn.

Gunicorn imports `app` from this module:

    gunicorn --bind 0.0.0.0:8000 wsgi:app
"""

from app import create_app


app = create_app()
