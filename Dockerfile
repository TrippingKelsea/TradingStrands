FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock LICENSE ./

# Install production dependencies only
RUN uv sync --frozen --no-dev

# Copy source code
COPY src/ ./src/

# Create non-root user and set ownership
RUN useradd --system --create-home --shell /bin/bash app \
    && chown -R app:app /app

USER app

EXPOSE 8080

# Health check for dashboard variant
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default entrypoint runs trading service.
# ECS task definition should pass --strategy <name> as CMD.
ENTRYPOINT ["uv", "run", "python", "-m", "trading_strands.app"]
