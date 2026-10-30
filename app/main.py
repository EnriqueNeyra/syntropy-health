"""
Syntropy Health — application entry point.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import agents, assistant, biometrics, connections, data, desktop, directory, insights, labs, profiles, records, settings, system
from app.core import auth, config, data_fixes, db, maintenance, security, settings as instance_settings
from app.core.uploads import DecompressUploads
from app.services import network, scheduler, updates
from app.store import biometrics as biometric_store, sample_counts, search
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


class _QuietHealthChecks(logging.Filter):
    """A container's health check every 30 seconds would bury everything else in the access log. Failures still show."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 5 and args[2] == "/health" and args[4] == 200
                    and str(args[0]).startswith(("127.0.0.1", "[::1]", "::1")))


logging.getLogger("uvicorn.access").addFilter(_RedactSecrets())
logging.getLogger("uvicorn.access").addFilter(_QuietHealthChecks())

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


def _start_up() -> None:
    db.ensure_migrated()
    security.check_key()
    instance_settings.migrate_client_ids()
    biometric_store.upgrade_daily_if_needed()
    sample_counts.warm()
    maintenance.check_at_start_up()
    search.at_start_up()


def _database_trouble(exc: Exception) -> Optional[tuple[int, str]]:
    """When a request failed because of the database rather than a bug: the status, and what to say."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return None
    if db.is_damage(exc):
        maintenance.note_damage(str(exc))
        return 500, ("Syntropy Health found damage in its database file, so this couldn't be read. Keep your older backups: "
                     "they may be the last good copies.")
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    code = db.error_code(exc)
    if db.is_busy(exc) or "locked" in str(exc):
        return 503, "The database is busy with a long task. Try again in a minute."
    if code == sqlite3.SQLITE_FULL:
        return 503, "The disk is full, so this couldn't be saved. Free up some space and try again."
    if code == sqlite3.SQLITE_READONLY:
        return 503, "Syntropy Health can't write to its database. Check the data folder's permissions."
    if code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_IOERR):
        return 503, "Syntropy Health can't get to its database. Check that the data folder is there and its disk is connected."
    return None


def _problem_page(problem: str, here: bool) -> str:
    try:
        folder = f"<p>Your data folder is {html.escape(str(config.data_dir()))}.</p>" if here else ""
    except OSError:
        folder = ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{config.APP_NAME} can't start</title><style>:root{{color-scheme:light dark}}
body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:36rem;margin:0 auto;padding:12vh 16px;overflow-wrap:anywhere}}
h1{{font-size:1.3rem;margin:0 0 .75rem}}</style></head>
<body><h1>{config.APP_NAME} can't start</h1><p>{html.escape(problem)}</p>
{folder}</body></html>"""


def _problem_response(request: Request, problem: str) -> Response:
    path = request.url.path
    here = network.from_this_computer(request)
    problem = problem if here else maintenance.FROM_ELSEWHERE
    if path == "/health":
        return JSONResponse({"status": "error", "service": "syntropy-health", "version": config.APP_VERSION,
                             "journal_mode": None, "detail": problem}, status_code=503)
    if path.startswith("/api/") or path == "/mcp":
        return JSONResponse({"detail": problem}, status_code=503, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_problem_page(problem, here), status_code=503, headers={"Cache-Control": "no-store"})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.problem = None
    try:
        await asyncio.to_thread(_start_up)
    except db.StartupProblem as exc:
        app.state.problem = str(exc)
    except sqlite3.DatabaseError as exc:        # such as damage that only shows once the data is read
        app.state.problem = str(db.unusable(exc, config.db_path()))
    except OSError as exc:
        log.exception("Couldn't start")
        app.state.problem = (f"Syntropy Health can't use its data folder: {exc}. Check that the disk has room and that this "
                             "account may write to that folder, then start it again.")
    if app.state.problem:
        # Up, but touching nothing: the desktop window and browsers can say why, a container's health check fails, and
        # a restart policy doesn't turn it into a crash loop with no explanation.
        log.error("Not starting: %s", app.state.problem)
        yield
        return
    maintenance.start()
    data_fixes.start()
    if config.scheduler_enabled():
        scheduler.start()
        updates.start()
    network.start_announcing()
    yield
    network.stop_announcing()
    await scheduler.stop()
    await updates.stop()
    await asyncio.to_thread(data_fixes.stop)
    await asyncio.to_thread(maintenance.stop)


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
        if ((request.url.path.startswith("/api/") or request.url.path == "/mcp") and not network.is_local_name(request.headers.get("host", ""))
                and not auth.auth_required()):
            return JSONResponse({"detail": "This address isn't allowed without a password. Set a password, or add it "
                                           "to SYNTROPY_ALLOWED_HOSTS."}, status_code=421)
        return await call_next(request)

    @app.middleware("http")
    async def explain_a_startup_problem(request: Request, call_next):
        problem = getattr(request.app.state, "problem", None)
        return _problem_response(request, problem) if problem else await call_next(request)

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

    @app.exception_handler(db.StartupProblem)
    async def startup_problem(request: Request, exc: db.StartupProblem) -> Response:
        return _problem_response(request, str(exc))

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        # A bug, not something the person did: keep the details in the server log, and give the page a sentence it can
        # show rather than a bare "Internal Server Error".
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        known = _database_trouble(exc)
        if known:
            return JSONResponse({"detail": known[1]}, status_code=known[0], headers={"Cache-Control": "no-store"})
        return JSONResponse({"detail": "Something went wrong on the server. Try again; if it keeps happening, the server "
                                       "log has the details."}, status_code=500, headers={"Cache-Control": "no-store"})

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> Response:
        if request.url.path.startswith(("/api/", "/sim/", "/static/")) or "/.well-known/" in request.url.path:
            detail = getattr(exc, "detail", "Not found")
            return JSONResponse({"detail": detail}, status_code=404)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
