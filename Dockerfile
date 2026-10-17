# Demo deployment image: standard library only, local Qwen inference disabled.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AAC_HOST=0.0.0.0 \
    AAC_PORT=8765 \
    AAC_DB_PATH=/app/var/aac.sqlite3 \
    AAC_LOCAL_MODEL=off

WORKDIR /app
RUN useradd --create-home --uid 1000 aac && mkdir -p /app/var && chown aac:aac /app/var
COPY aac/ aac/
COPY web/ web/

USER aac
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/', timeout=3)"
CMD ["python", "-m", "aac"]
