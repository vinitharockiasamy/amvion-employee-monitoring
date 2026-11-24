FROM python:3.12-slim

WORKDIR /app

# Copy requirements and install server dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi uvicorn[standard] sqlalchemy jinja2 python-multipart aiofiles websockets httpx pydantic

# Copy application code and signed Windows MSI installer package
COPY server ./server
COPY EMP-SA-enterprise-1.0.0.msi ./EMP-SA-enterprise-1.0.0.msi
COPY run_server.py .

EXPOSE 8000

CMD ["python", "run_server.py"]
