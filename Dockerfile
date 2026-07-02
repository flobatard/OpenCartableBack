FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1
# Si un wheel ARM64 manque : apt-get install -y build-essential ici (le runtime reste propre)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
RUN useradd --create-home appuser
WORKDIR /srv/app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY config/ ./config/
COPY app/ ./app/
COPY scripts/init_db.py ./scripts/init_db.py
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh
USER appuser
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
