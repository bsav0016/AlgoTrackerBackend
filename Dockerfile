FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# build-essential covers any dependency that doesn't ship a prebuilt wheel
# for this platform; psycopg2-binary itself bundles libpq and needs neither.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Settings.py requires real secrets (Firebase credentials, SYMBOLS, etc.) to
# import successfully, which aren't available at build time — so collectstatic
# and migrate run at container start (see docker-compose.yml / deploy steps),
# not here.
CMD ["gunicorn", "algo_tracker_backend.wsgi", "--bind", "0.0.0.0:8000", "--log-file", "-"]
