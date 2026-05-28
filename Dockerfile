# Container image for the MiniTen Flask API/dashboard process.
FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies before copying the full source tree so Docker can
# reuse this layer when application code changes but requirements do not.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV FLASK_APP=app

# The development image runs Flask directly. Production deployment can replace
# this with a WSGI server command without changing application code.
CMD ["flask", "run", "--host", "0.0.0.0"]
