FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Stream logs straight to Railway instead of buffering them.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Bind the exposed port directly. Railway routes to 8000 for this service, so
# do not swap in $PORT: if the platform sets PORT to something else while still
# routing to 8000, the proxy gets nothing and every request comes back 502.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
