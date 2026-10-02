"""
Syntropy Health — application entry point.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import agents, assistant, biometrics, connections, data, desktop, directory, insights, labs, profiles, records, settings, system
from app.core import auth, config, db
from app.core.uploads import DecompressUploads
from app.services import network, scheduler, updates
from app.store import biometrics as biometric_store, sample_counts
from app.simulator import server as simulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# OAuth codes and tokens, and search terms (which may name a condition or medication).
_SECRET_PARAMS = re.compile(r"([?&](?:code|state|token|access_token|id_token|device_token|q|query|search)=)[^&\s]+", re.I)


class _RedactSecrets(logging.Filter):
    """Keeps OAuth codes and tokens that arrive in query strings (e.g. /callback), and what was searched for, out of
    the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            record.args = (*record.args[:2], _SECRET_PARAMS.sub(r"\1REDACTED", record.args[2]), *record.args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactSecrets())

STATIC_DIR = config.APP_DIR / "static"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
# Browser features the app never uses, turned off for its pages and anything that might ever be injected into them.
PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), hid=(), "
                      "midi=(), magnetometer=(), gyroscope=(), accelerometer=(), browsing-topics=()")
# Pages that set their own policy: the simulator's sign-in forms (which post, then redirect to the relay) and the
# OAuth hand-off page (see app.api.connections).
_OWN_CSP = ("/sim/", "/callback")

log = logging.getLogger("syntropy")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.ensure_migrated()
    biometric_store.upgrade_daily_if_needed()
    sample_counts.warm()
    if config.scheduler_enabled():
        scheduler.start()
        updates.start()
    network.start_announcing()
    yield
    network.stop_announcing()
    await scheduler.stop()
    await updates.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Syntropy Health",
        version=config.APP_VERSION,
        description="Self-hosted personal health record: EHRs (SMART on FHIR), wearables and imports in one local database.",
        docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def local_names_without_password(request: Request, call_next):
        # Without a password, the API answers only to local names (localhost, home network, Tailscale): otherwise a web
        # page could use DNS rebinding to read records from a browser on this network. With a password, its session
        # cookie belongs to the real address, so the password itself stops that.
        if (request.url.path.startswith("/api/") and not network.is_local_name(request.headers.get("host", ""))
                and not auth.auth_required()):
            return JSONResponse({"detail": "This address isn't allowed without a password. Set a password, or add it "
                                           "to SYNTROPY_ALLOWED_HOSTS."}, status_code=421)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        path = request.url.path
        html = response.headers.get("content-type", "").startswith("text/html")
        # Every page of the app, including addresses that fall back to it (see not_found below).
        if path == "/" or path.startswith("/static/") or (html and not path.startswith(_OWN_CSP)):
            response.headers.setdefault("Content-Security-Policy", CSP)
        if path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/") and "cache-control" not in response.headers:
            # Unversioned ES modules: revalidate every load (a cheap 304) so an upgrade never mixes old and new
            # scripts. Fonts never change and can be cached for good.
            response.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if path.startswith("/static/fonts/")
                                                 else "no-cache")
        return response

    app.add_middleware(DecompressUploads)

    for module in (system, profiles, directory, settings, connections, records, biometrics, insights, data, labs, assistant, agents, desktop):
        app.include_router(module.router)
    app.include_router(agents.mcp_router)
    app.include_router(simulator.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/privacy", include_in_schema=False)
    async def privacy() -> Response:
        return Response(status_code=307, headers={"Location": "https://health.syntropylabs.io/privacy"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        # Same body as FastAPI's default, but echoed inputs such as NaN/Infinity (which are not valid
        # JSON) become null instead of turning the 422 into a 500.
        return JSONResponse({"detail": db.finite(jsonable_encoder(exc.errors()))}, status_code=422)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        # A bug, not something the person did: keep the details in the server log, and give the page a sentence it can
        # show rather than a bare "Internal Server Error".
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Something went wrong on the server. Try again; if it keeps happening, the server "
                                       "log has the details."}, status_code=500, headers={"Cache-Control": "no-store"})

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> Response:
        if request.url.path.startswith(("/api/", "/sim/", "/static/")):
            detail = getattr(exc, "detail", "Not found")
            return JSONResponse({"detail": detail}, status_code=404)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
