CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**2. Add a Railway variable** — in Railway, click **Variables** tab on your service and add:
```
PORT = 8000
