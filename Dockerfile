FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN mkdir -p /app/data
ENV DATABASE_PATH=/app/data/monitor.db PORT=8080
EXPOSE 8080
CMD ["python", "-m", "yc_monitor", "serve"]
