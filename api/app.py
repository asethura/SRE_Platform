"""SRE Platform UI backend -- FastAPI service exposing the Fleet, FinOps,
and Tasks tabs, and serving the built React SPA (ui/dist) as static files.

Reuses hitl.py's approve()/reject() verbatim for the Tasks tab -- no
approval logic is reimplemented here. See k8s_fleet.py for the only place
this service touches the Kubernetes API (read-only, RBAC in
k8s/base/api-rbac.yaml).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routers import approvals, finops, fleet, incidents

app = FastAPI(title="SRE Platform UI")

# No auth exists on this service (or anything to protect against) -- it's
# reached only via `kubectl port-forward`, never exposed publicly. CORS is
# wide open purely so `vite dev` (a different origin) can talk to a
# separately-run `uvicorn` during local development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(fleet.router)
app.include_router(finops.router)
app.include_router(approvals.router)
app.include_router(incidents.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIST, html=True), name="ui")
