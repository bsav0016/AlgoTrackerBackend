FROM python:3.12-slim

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

# Static files (the Django admin and the browsable API) are collected into the image, so whitenoise can serve them
# and its manifest exists. settings.py needs real secrets to import normally, so the build gives it a throwaway
# SECRET_KEY and DEBUG=True (which makes the Firebase credentials optional); these values exist only for this one
# command and are not in the running container's environment.
RUN SECRET_KEY=build-only-not-a-secret DEBUG=True python manage.py collectstatic --noinput

# Database migrations are not run here (no database at build time). docker-compose.yml runs them in the one-shot
# `migrate` service before the app services start.
# Workers, threads and timeouts live in gunicorn.conf.py (settings can be overridden with GUNICORN_* variables).
CMD ["gunicorn", "algo_tracker_backend.wsgi", "--config", "gunicorn.conf.py"]
