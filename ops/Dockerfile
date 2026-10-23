FROM python:3.11-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libopus0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.30
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --extra red --no-dev
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

FROM runtime AS worker
USER root
RUN printf '%s\n' 'export PATH="/app/.venv/bin:$PATH"' 'export UV_PROJECT_ENVIRONMENT=/app/.venv' \
    > /etc/profile.d/gene-venv.sh
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
RUN uv sync --frozen --extra red
COPY tests ./tests
COPY AGENTS.md ./
RUN chmod -R a+rX /app/tests
RUN git config --system --add safe.directory /workspace
USER 10001:10001
CMD ["codex", "--version"]

