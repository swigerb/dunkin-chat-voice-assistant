# Stage 1: Build the Vite app
FROM node:22-slim AS build-stage

# Set the working directory inside the container
WORKDIR /frontend

COPY frontend ./

# Create a safe default .env if one is not present.
# Note: Avoid ARG/ENV for these values to prevent accidental secret leakage.
RUN if [ ! -f .env ]; then \
      printf "VITE_AUTH_URL=\nVITE_AUTH_ENABLED=false\n" > .env; \
    fi

RUN npm ci
RUN npm run build

# Stage 2: Serve the built app with a static file server
FROM python:3.12-slim AS production-stage

WORKDIR /app
COPY ./backend/ /app

# Provide menu metadata to the backend at runtime (used for extras/category rules).
RUN mkdir -p /app/data
COPY ./frontend/src/data/menuItems.json /app/data/menuItems.json

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN python -m pip install -r requirements.txt

# Copy the freshly-built frontend assets last so they are not overwritten by the backend source copy.
COPY --from=build-stage /backend/static /app/static

# Production gunicorn config:
# - exactly one worker: orders and the order-resume grace hold live in process memory
# - 120s timeout for long-lived WebSocket upgrades
# - 65s keep-alive to outlast Azure LB idle timeout (60s)
# - Graceful 30s shutdown window
CMD ["python3", "-m", "gunicorn", "app:create_app", "-b", "0.0.0.0:8000", "--workers", "1", "--worker-class", "aiohttp.GunicornWebWorker", "--timeout", "120", "--keep-alive", "65", "--graceful-timeout", "30"]
