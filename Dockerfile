FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY training_bot ./training_bot
RUN pip install . && useradd --create-home --uid 10001 app && mkdir /app/data && chown app:app /app/data
USER app
ENTRYPOINT ["training-bot"]
CMD ["status"]
