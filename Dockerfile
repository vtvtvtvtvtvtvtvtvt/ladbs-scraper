FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Stream logs straight to Railway instead of buffering them.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Railway injects $PORT; fall back to 8000 for local runs.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
