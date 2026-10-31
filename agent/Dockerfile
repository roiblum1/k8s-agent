# The upgrade agent service. Build from THIS directory (agent/):
#   podman build -t upgrade-agent:dev .
# Runs as a non-root user and works under OpenShift's arbitrary UID: files
# are owned by group 0 and group-writable where the process writes (/tmp,
# the state volume is mounted by the chart).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY upgrade_agent/ /app/upgrade_agent/
RUN chgrp -R 0 /app && chmod -R g=u /app

USER 1001
EXPOSE 8080
CMD ["uvicorn", "upgrade_agent.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
