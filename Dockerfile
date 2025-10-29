FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .


# Default for local; Railway injects PORT at runtime
ENV PORT=8000


# Use a shell so ${PORT} expands when Docker CMD runs.
CMD ["sh","-c","python -c 'import os, uvicorn; uvicorn.run(\"app:app\", host=\"0.0.0.0\", port=int(os.getenv(\"PORT\", \"8000\")))'"]
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
