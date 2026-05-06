# ADR 0004: FastAPI + Static Frontend over Streamlit

- **Status:** Accepted
- **Date:** 2026-05-05
- **Supersedes:** initial Streamlit prototype (commit `4d65296`)

## Context

The first iteration of the dashboard used **Streamlit** — fast to build, single-file, Python-only. It worked, but as we approached production-readiness several concerns surfaced:

| Concern | Streamlit | Cost |
|---|---|---|
| Multi-user / scale-out | Single session per process | Doesn't scale horizontally |
| API contract | None — UI logic only | No way for another client (mobile, BI tool, internal service) to consume the analysis |
| Testability | Hard to unit-test the UI logic | Coverage gap |
| Deployment target | Streamlit-specific runtime | Doesn't fit standard ASGI / Docker / Kubernetes patterns |
| Frontend flexibility | Streamlit components only | Can't iterate on UX without working in Streamlit's idiom |

These costs accumulate the moment the project moves beyond a single-user demo.

## Decision

We **replaced Streamlit with FastAPI + a vanilla HTML/Plotly.js static frontend**.

- FastAPI service in `api/` exposes the analysis as a REST API at `/api/v1/*`
- Pydantic response models → OpenAPI schema → auto-generated docs at `/docs`
- Static frontend in `web/` — no build step, vanilla JS, Plotly.js via CDN
- The web dashboard at `/` consumes the same JSON endpoints any external client would
- Pipeline state cached in a thread-safe singleton (`api/state.py`)

## Consequences

**Positive**
- **Stateless service** — scales horizontally behind a load balancer
- **Versioned API contract** — `/api/v1/*` with Pydantic schemas; future v2 ships side-by-side
- **Testable end-to-end** — FastAPI's `TestClient` covers every endpoint (14 dedicated tests)
- **Standard deployment** — uvicorn + Docker + any cloud (ECS, Cloud Run, GKE, Fly, etc.)
- **Frontend flexibility** — replace the vanilla SPA with React/Svelte/whatever later without touching the API
- **No build step on the frontend** — keeps the dev loop tight; production-deployable as static assets

**Negative**
- More files than a single `streamlit_app.py`. Mitigated by clear module boundaries (`api/main.py`, `routes.py`, `models.py`, `state.py`)
- Lost some Streamlit-specific features (auto-rerun on file change). Mitigated by `uvicorn --reload` for dev

**Neutral**
- The notebook (`transcript_intelligence.ipynb`) remains the narrative deliverable. The dashboard is for live demo and drill-downs.

## When to revisit

- Should not need to. This decision is the *foundation* — a v2 dashboard with a real frontend framework (React + Vite) layers on top without changing the backend.

## Related

- `api/main.py` — middleware stack, lifespan, routing
- `api/routes.py` — `/api/v1/*` endpoints
- `web/index.html` + `web/static/app.js` — static frontend
- `tests/test_api.py` — end-to-end API tests
