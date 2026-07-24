# Testing

The key-free suite covers authentication, authorization, CSRF, stage order, idempotency, persistence, synthesis, annotations, interface source checks, and reasoning-trace removal.

```bash
./run.sh test
```

This runs full `unittest` discovery across `tests/test_*.py`.

## Local authenticated simulation

The simulation exercises the same account and record routes as the browser:

`register → verify → sign in → create a record → complete seven stages → synthesize → annotate`

Start the development server with the memory email adapter. The deterministic model adapter keeps the run key-free:

```bash
APP_ENV=development \
EMAIL_BACKEND=memory \
MODEL_BACKEND=stub \
PUBLIC_APP_URL=http://127.0.0.1:8765 \
AUTH_PEPPER=local-development-only-pepper-change-before-production \
python3 server.py
```

In another terminal:

```bash
python3 tests/simulate.py
python3 tests/simulate.py --verbose
```

The harness accepts loopback HTTP URLs only. It registers a unique synthetic user, reads the loopback-only memory outbox, and follows the normal verification-token flow. Tokens and passwords stay in memory and never appear in output.

Set `MODEL_BACKEND=ollama` and `OLLAMA_API_KEY` on the local server to exercise live model responses. Wording may vary; the harness checks saved state, required response counts, graph creation, annotations, and reasoning-trace boundaries.

Production has no development outbox or stub model, so the simulation cannot run against a deployed service.
