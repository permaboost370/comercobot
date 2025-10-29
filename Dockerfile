FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Railway injects PORT automatically; we’ll read it in start.py
ENV PORT=8000

# Run the lightweight launcher
CMD ["python", "start.py"]
