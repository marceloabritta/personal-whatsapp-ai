# ============================================================================
#  Dispatcher — the single webhook target that fans out to the flows by @tag.
#  See app.py. Evolution's instance webhook points here; it forwards to
#  Mary flow (:3000) and Lisa flow (:8000) on the shared evolution-net.
# ============================================================================
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
