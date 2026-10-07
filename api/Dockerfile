FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime identity. 1000:1000 is the existing `logplatform` service
# account: it owns the checkout and runs the native systemd API, so both API
# deployments share one identity and the development bind mount stays readable
# without relaxing host modes.
ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    if ! getent group "${APP_GID}" >/dev/null; then groupadd --gid "${APP_GID}" app; fi; \
    if ! getent passwd "${APP_UID}" >/dev/null; then \
      useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin app; \
    fi

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Application source stays root-owned and is never writable by the runtime user,
# so the container cannot rewrite its own code. Modes are normalized because
# COPY preserves build-context modes and four eco_driving_explorer admin modules
# are 0600 in the checkout; unreadable to a non-root runtime user they would fail
# the unguarded imports in main.py.
RUN set -eux; \
    chown -R root:root /app; \
    find /app -type d -exec chmod 0755 {} +; \
    find /app -type f -exec chmod 0644 {} +

USER ${APP_UID}:${APP_GID}

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
