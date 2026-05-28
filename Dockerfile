# Container image for the MiniTen Flask API/dashboard process.
FROM python:3.12-slim

WORKDIR /app

# Install Poetry and project dependencies before copying the full source tree so
# Docker can reuse this layer when application code changes but dependencies do
# not.
RUN pip install --no-cache-dir poetry
# Copy the lock file with pyproject so dependency installs are reproducible.
COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi

COPY . .

ENV PYTHONUNBUFFERED=1
ENV API_PORT=8000

# Run the API with Gunicorn by default. The worker service overrides this
# command in docker-compose.yml.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "wsgi:app"]
