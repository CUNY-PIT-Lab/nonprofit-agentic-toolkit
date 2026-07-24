# Toolkit backend

FastAPI serves the interface and account API from one origin. PostgreSQL stores verified users, organization memberships, adoption records, stage conversations, completed steps, model runs, knowledge snippets, synthesis versions, concept-map versions, annotations, and audit events. SQLite and in-memory email are local-development fallbacks.

## Local run

```bash
python3 -m pip install -r requirements.txt
./run.sh
```

Without an Ollama key, `run.sh` selects the deterministic local model adapter. Verification email remains required. Open `/api/dev/outbox` on the loopback server to retrieve the local verification or reset link, then follow the normal token flow. The outbox route and model adapter are unavailable in production.

Run the key-free checks with:

```bash
./run.sh test
```

## Production environment

- `APP_ENV=production`
- `DATABASE_URL` from the private Railway PostgreSQL service
- `PUBLIC_APP_URL`, fixed to the canonical HTTPS toolkit origin
- `EMAIL_BACKEND=resend`
- `RESEND_API_KEY`
- `MAIL_FROM`, using a verified sending domain
- `AUTH_PEPPER` or `SESSION_SECRET`, at least 32 random characters
- `MODEL_BACKEND=ollama`
- `OLLAMA_API_KEY`
- `TOOLKIT_MODEL=glm-5.2`

Registration returns `503` until the production email configuration is complete. The backend also refuses PostgreSQL-free production startup, a non-HTTPS public URL, an absent authentication pepper, or the local model adapter.

## Browser contract

Call `GET /api/auth/session` first. It sets a readable CSRF cookie and returns the same token as `csrf_token`. Every state-changing request must send the exact application `Origin`, cookies, and `X-CSRF-Token`. Production uses host-only `__Host-` cookie names with `Secure`, `SameSite=Lax`, and root paths.

Verification and reset links place the opaque token in the URL fragment:

```text
/#verify?token=...
/#reset?token=...
```

The browser extracts the fragment and submits the token to `POST /api/auth/verify` or `POST /api/auth/reset-password`. Tokens do not enter query strings or routine access logs.

Stage messages use `POST /api/records/{record_id}/stages/{stage}/messages` with `content` and an `idempotency_key`. The browser creates one stable key for each submission and reuses it when retrying the same request. The server enforces stage order and loads membership, history, completed-stage context, and the system prompt. Synthesis is available after every review stage is complete.
