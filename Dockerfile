FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY server.py ./

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=3)"

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]

