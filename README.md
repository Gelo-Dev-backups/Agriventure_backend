# AgriVenture Backend

FastAPI backend for the AgriVenture smart-farming app. Frontend-agnostic
(Flutter, React, Vue, etc.) — talk to it over plain HTTPS/JSON.

## 1. Setup

```bash
cp .env.example .env        # fill in DATABASE_URL and JWT secrets
pip install -r requirements.txt
uvicorn main:app --reload
```

Interactive API docs: `http://localhost:8000/docs`

## 2. Deploy (Docker)

```bash
docker build -t agriventure-backend .
docker run -p 8000:8000 --env-file .env agriventure-backend
```

Point any reverse proxy (Nginx, Caddy, your cloud provider's load balancer)
at port 8000 and terminate TLS there — the app itself is HTTP; HTTPS is
handled at the edge, as is standard for containerized FastAPI services.

## 3. Project structure

```
agriventure_backend/
├── main.py            # FastAPI app, middleware, router wiring, error handling
├── config.py           # env loading, DB connection pool, JWT/security settings
├── schemas.py           # every Pydantic request/response model (DTOs)
├── requirements.txt
├── Dockerfile
├── .env.example
├── routes/
│   ├── auth.py          # register / verify-otp / login / refresh / password reset
│   ├── farms.py         # farm CRUD (ownership-scoped)
│   ├── sensors.py       # sensor CRUD + IoT reading ingestion (single + bulk)
│   ├── crop_analysis.py # image upload -> AI inference -> analysis/history/recs
│   ├── recommendations.py
│   ├── notifications.py
│   └── dashboard.py     # single aggregate endpoint for the home screen
└── utils/
    ├── security.py       # bcrypt hashing, JWT encode/decode, OTP helpers
    ├── deps.py            # get_current_user auth guard, ownership helpers
    ├── mailer.py          # OTP/email delivery abstraction (stub -> real SMTP)
    └── pagination.py
```

## 4. Auth flow

1. `POST /api/v1/auth/register` — creates user, emails a 6-digit OTP.
2. `POST /api/v1/auth/verify-otp` — activates the account.
3. `POST /api/v1/auth/login` — returns `access_token` (short-lived) +
   `refresh_token` (long-lived).
4. Send `Authorization: Bearer <access_token>` on every protected request.
5. `POST /api/v1/auth/refresh` — trade a refresh token for a new pair
   (rotation) once the access token expires.
6. `POST /api/v1/auth/forgot-password` / `reset-password` — OTP-based reset.

All responses share one envelope:

```json
{ "success": true, "message": "OK", "data": { ... } }
```

Errors:

```json
{ "success": false, "message": "Invalid email or password" }
```

## 5. Key endpoints by frontend screen

| Flutter screen        | Endpoints |
|------------------------|-----------|
| Dashboard              | `GET /api/v1/dashboard/summary` |
| Farms                  | `GET/POST/PATCH/DELETE /api/v1/farms` |
| Add Sensor              | `POST /api/v1/sensors`, `GET /api/v1/sensors?farm_id=` |
| (IoT devices)           | `POST /api/v1/sensors/readings`, `/readings/bulk` |
| AI Camera Scanner       | `POST /api/v1/crop-analysis/upload` (multipart form: `farm_id`, `file`) |
| Recommendation          | `GET /api/v1/recommendations` |
| Notifications           | `GET /api/v1/notifications`, `PATCH /{id}/read` |
| User Profile            | `GET /api/v1/auth/me`, `POST /api/v1/auth/change-password` |

## 6. Security notes / production hardening checklist

- [x] bcrypt password hashing, never plaintext.
- [x] JWT access + refresh tokens signed with separate secrets.
- [x] Rate limiting on login/OTP/password-reset endpoints (slowapi).
- [x] Ownership checks on every farm/sensor/analysis query.
- [x] Parameterized SQL everywhere (no string-built queries) — SQL-injection safe.
- [x] Consistent error envelope that never leaks stack traces to clients.
- [ ] Swap the `utils/mailer.py` stub for real SMTP/SES/SendGrid before launch.
- [ ] Swap local disk uploads for S3/Cloud Storage for horizontal scaling.
- [ ] Add a `revoked_tokens` table if you need hard "logout everywhere".
- [ ] Put this behind HTTPS (managed by your cloud provider / reverse proxy).
- [ ] Restrict `CORS_ORIGINS` in `.env` to your real app domains in production.
