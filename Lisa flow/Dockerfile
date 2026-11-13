FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
# Exec form, so uvicorn is PID 1 and gets Docker's SIGTERM itself. On it uvicorn stops taking
# connections and waits for the turns in flight; after 100 seconds it cancels what is left and
# runs the teardown. 100 covers the longest turn in a month of the log (81 s, a web answer; no
# calendar turn passed 23 s). The service's stop_grace_period in compose has to be longer than
# this, or Docker's SIGKILL comes first and nothing is waited for.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "100"]
