FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM build AS gateway-build
RUN uv sync --locked --no-dev --extra gateway --no-install-project

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
ARG BUILD_SHA=development
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BUILD_SHA=$BUILD_SHA
LABEL org.opencontainers.image.source="https://github.com/henrique-meyer/pipmind" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision=$BUILD_SHA
WORKDIR /app
RUN groupadd --gid 10001 pipmind && useradd --uid 10001 --gid pipmind --no-create-home pipmind
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY web ./web
COPY assets/logo.svg assets/logo-mark.svg ./assets/
COPY LICENSE ./LICENSE
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--no-access-log"]

FROM runtime AS gateway
COPY --from=gateway-build /app/.venv /app/.venv
COPY pipmind ./pipmind
COPY contracts/v1 ./contracts/v1
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3)"
CMD ["python", "-m", "pipmind.gateway", "serve", "--host", "0.0.0.0"]

# The default target stays the public landing application.
FROM runtime AS landing
