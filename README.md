## Local development

1. Copy `.env.example` to `.env` and fill in real values (get `TWELVE_DATA_API_KEY`,
   Stripe test keys, etc. from their respective dashboards).
2. Place your Firebase service-account JSON at the path referenced by
   `FIREBASE_CREDENTIALS_PATH` in `.env`.
3. `docker compose up --build`

   This runs the full stack: Django (`web`), the RQ worker (`worker-tasks`),
   the TwelveData websocket listener (`worker-websocket`), Postgres, Redis,
   and Caddy as a reverse proxy.
4. On first run, apply migrations and create an admin user:
   ```
   docker compose exec web python manage.py migrate
   docker compose exec web python manage.py createsuperuser
   ```
5. In the frontend's `lib/networkRequests/NetworkConstants.tsx`, point the
   `URL` constant at wherever Caddy is listening (e.g. `http://localhost:8000`
   if you're bypassing Caddy locally, or the `DOMAIN` you set in `.env`).

## Production deployment

Runs on a single DigitalOcean Droplet via the same `docker-compose.yml`.
Deploys are manual for now: SSH in, `git pull`, `docker compose up --build -d`.
