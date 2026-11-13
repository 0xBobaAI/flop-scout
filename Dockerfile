FROM python:3.12-slim

WORKDIR /app

# System deps for sr25519 (Rust bindings)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Never copy secrets into the image
RUN rm -f .wallet.json .wallet.backup.json log.db

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
