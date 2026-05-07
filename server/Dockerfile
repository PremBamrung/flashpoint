FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml .
RUN uv sync --frozen --no-dev

COPY app/ ./app/

EXPOSE 8000

CMD ["uv", "run", "python", "-m", "app.main"]
