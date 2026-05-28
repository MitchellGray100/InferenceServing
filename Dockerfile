# Container image for the MiniTen Flask API/dashboard process.
FROM python:3.12-slim

WORKDIR /app

# Install Poetry and project dependencies before copying the full source tree so
# Docker can reuse this layer when application code changes but dependencies do
# not.
RUN pip install --no-cache-dir poetry
COPY pyproject.toml .
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi

COPY . .

ENV FLASK_APP=app

# The development image runs Flask directly. Production deployment can replace
# this with a WSGI server command without changing application code.
CMD ["flask", "run", "--host", "0.0.0.0"]
