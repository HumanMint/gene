FROM python:3.11-slim-bookworm AS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libopus0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.30
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Source edits must reuse the locked third-party environment, not reinstall Red.
RUN uv sync --frozen --extra red --no-dev --no-install-project --no-cache

FROM dependencies AS audio-dependencies
COPY ops/audio-adapter.json /opt/audio-adapter-pin.json
COPY scripts/install_audio_adapter.py /tmp/install_audio_adapter.py
# The Red/worker images need this bundle; the host controller wrappers do not.
# Keeping it outside the managed site-packages preserves exact core syncs.
RUN /app/.venv/bin/python /tmp/install_audio_adapter.py --pin /opt/audio-adapter-pin.json --target /opt/red-audio-adapter

FROM audio-dependencies AS runtime
COPY src ./src
RUN uv sync --frozen --extra red --no-dev --no-cache
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home gene
COPY cogs ./cogs
# Private release staging can give source directories restrictive modes. Keep
# checked-in code root-owned and readable by the unprivileged runtime user.
RUN chmod -R a+rX /app/src /app/cogs
USER 10001:10001
CMD ["gene-controller"]

FROM node:24-bookworm-slim AS codex
RUN npm install -g @openai/codex@0.160.1 --ignore-scripts

FROM audio-dependencies AS worker-dependencies
# Worker tools and development dependencies must also survive source-only edits.
RUN uv sync --frozen --extra red --no-install-project --no-cache
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home gene
RUN printf '%s\n' 'export PATH="/app/.venv/bin:$PATH"' 'export UV_PROJECT_ENVIRONMENT=/app/.venv' \
    > /etc/profile.d/gene-venv.sh
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
RUN git config --system --add safe.directory /workspace

FROM worker-dependencies AS worker
COPY src ./src
RUN uv sync --frozen --extra red --no-cache
COPY cogs ./cogs
COPY tests ./tests
COPY AGENTS.md ./
RUN chmod -R a+rX /app/src /app/cogs /app/tests
USER 10001:10001
CMD ["codex", "--version"]

