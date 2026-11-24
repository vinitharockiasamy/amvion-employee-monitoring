FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Copy requirements and install production server dependencies (including PostgreSQL driver)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and signed Windows MSI installer package
COPY server ./server
COPY EMP-SA-enterprise-*.msi ./
COPY run_server.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

CMD ["python", "run_server.py"]
