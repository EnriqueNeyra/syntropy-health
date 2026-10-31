"""
Syntropy Health — application entry point.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from app.core.sqlite import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import agents, assistant, backups, biometrics, connections, data, desktop, directory, insights, labs, profiles, records, settings, system
from app.core import auth, config, data_fixes, db, encryption, keystore, maintenance, security, settings as instance_settings
from app.core import private_files
from app.core.uploads import DecompressUploads
from app.services import backups as backups_service, imports, network, renormalize, scheduler, updates, wearable_fixes  # noqa: F401 - renormalize and wearable_fixes register data fixes
from app.store import biometrics as biometric_store, profiles as profile_store, sample_counts, search
from app.simulator import server as simulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# OAuth codes and tokens, and search terms and filters (which may name a test, a condition, a medication or a doctor).
_SECRET_PARAMS = re.compile(r"([?&](?:code|state|token|access_token|id_token|device_token|q|query|search"
                            r"|title|author|note_type|type|category|measure|metric|a|b)=)[^&\s]+", re.I)


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

private_files.protect_upload_spool()

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
    backups_service.clean_leftovers()
    encryption.at_start_up()
    db.ensure_migrated()
    security.check_key()
    instance_settings.migrate_client_ids()
    biometric_store.resume_rebuilds()
    imports.recover_at_start_up()
    sample_counts.warm()
    maintenance.check_at_start_up()
    search.at_start_up()
    imports.sweep_uploads()
    scheduler.prune()
    if not instance_settings.get("setup.completed") or auth.password_missing():
        code = auth.setup_code()
        log.warning("Setting up from another network? Your one-time setup code is %s-%s. On this computer or your home "
                    "network, you don't need it.", code[:4], code[4:])


def _finish_deletes() -> None:
    try:
        profile_store.finish_deletes()
    except Exception:  # noqa: BLE001 - it runs again at the next start
        log.exception("Couldn't finish removing deleted data")


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


def _unlock_form(said: str = "") -> str:
    warning = f'<p role="alert"><strong>{html.escape(said)}</strong></p>' if said else ""
    return f"""{warning}<form method="post" action="/unlock"><p><label for="pw">The Syntropy Health password of this server's owner can bring the key back:</label></p>
<p><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required style="font:inherit;padding:.4rem;width:100%;box-sizing:border-box"></p>
<p><button type="submit" style="font:inherit;padding:.4rem 1rem">Unlock</button></p></form>"""


def _problem_page(problem: str, here: bool, extra: str = "") -> str:
    try:
        folder = f"<p>Your data folder is {html.escape(str(config.data_dir()))}.</p>" if here else ""
    except OSError:
        folder = ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{config.APP_NAME} can't start</title><style>:root{{color-scheme:light dark}}
body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:36rem;margin:0 auto;padding:12vh 16px;overflow-wrap:anywhere}}
h1{{font-size:1.3rem;margin:0 0 .75rem}}</style></head>
<body><h1>{config.APP_NAME} can't start</h1><p>{html.escape(problem)}</p>
{extra}{folder}</body></html>"""


def _can_unlock(app: FastAPI, here: bool) -> bool:
    """The key's lost, this is the computer it runs on, and a password can bring it back. Never from elsewhere: nobody should
    be able to try passwords against the key from across a network."""
    return here and getattr(app.state, "problem_kind", None) == "key" and bool(keystore.recovery_entries())


def _problem_response(request: Request, problem: str) -> Response:
    path = request.url.path
    here = network.from_this_computer(request)
    problem = problem if here else maintenance.FROM_ELSEWHERE
    if path == "/health":
        return JSONResponse({"status": "error", "service": "syntropy-health", "version": config.APP_VERSION,
                             "journal_mode": None, "detail": problem}, status_code=503)
    if path.startswith("/api/") or path == "/mcp":
        return JSONResponse({"detail": problem}, status_code=503, headers={"Cache-Control": "no-store"})
    extra = _unlock_form() if _can_unlock(request.app, here) else ""
    return HTMLResponse(_problem_page(problem, here, extra), status_code=503, headers={"Cache-Control": "no-store"})


def _begin_work(app: FastAPI) -> None:
    """On the event loop: the scheduler's tasks belong to it."""
    app.state.working = True
    maintenance.start()
    data_fixes.start()
    threading.Thread(target=_finish_deletes, name="finish-deletes", daemon=True).start()
    if config.scheduler_enabled():
        scheduler.start()
        updates.start()
    network.start_announcing()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.problem = None
    try:
        await asyncio.to_thread(_start_up)
    except db.StartupProblem as exc:
        app.state.problem = str(exc)
        app.state.problem_kind = exc.kind
    except sqlite3.DatabaseError as exc:        # such as damage that only shows once the data is read
        problem = db.unusable(exc, config.db_path())
        app.state.problem, app.state.problem_kind = str(problem), problem.kind
    except OSError as exc:
        log.exception("Couldn't start")
        app.state.problem = (f"Syntropy Health can't use its data folder: {exc}. Check that the disk has room and that this "
                             "account may write to that folder, then start it again.")
    if app.state.problem:
        # Up, but touching nothing: the desktop window and browsers can say why, a container's health check fails, and
        # a restart policy doesn't turn it into a crash loop with no explanation.
        log.error("Not starting: %s", app.state.problem)
    else:
        _begin_work(app)
    yield
    if not getattr(app.state, "working", False):
        return
    network.stop_announcing()
    await asyncio.to_thread(imports.stop_running)
    await scheduler.stop()
    await updates.stop()
    await asyncio.to_thread(data_fixes.stop)
    await asyncio.to_thread(maintenance.stop)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Syntropy Health",
        version=config.APP_VERSION,
        description="Self-hosted personal health record: EHRs (SMART on FHIR), wearables and imports in one local database.",
        docs_url=None, redoc_url=None, openapi_url=None,
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
        if problem and not (request.url.path == "/unlock" and _can_unlock(request.app, network.from_this_computer(request))):
            return _problem_response(request, problem)
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

    for module in (system, profiles, directory, settings, connections, records, biometrics, insights, data, backups, labs, assistant, agents, desktop):
        app.include_router(module.router)
    app.include_router(agents.mcp_router)
    app.include_router(profiles.accounts_router)
    app.include_router(simulator.router, dependencies=[Depends(simulator.developers_only)])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.post("/unlock", include_in_schema=False)
    async def unlock(request: Request) -> Response:
        """The key's lost and the owner has their password: see app.core.security.recover_key. Only reachable while
        _can_unlock says so; otherwise there's nothing to unlock. The same guessing limits as signing in."""
        if not getattr(request.app.state, "problem", None):
            return Response(status_code=404)
        origin = request.headers.get("origin")
        if origin and origin.split("://")[-1] != request.headers.get("host"):     # another site's page posting a guess here
            return Response(status_code=403)
        try:
            auth.check_attempts(request, "unlock")
        except HTTPException as exc:
            return HTMLResponse(_problem_page(request.app.state.problem, True, _unlock_form(exc.detail)), status_code=429)
        auth.note_failure(request, "unlock")
        password = str((await request.form()).get("password") or "")
        if not await asyncio.to_thread(security.recover_key, password):
            return HTMLResponse(_problem_page(request.app.state.problem, True, _unlock_form("That password doesn't open it.")), status_code=401)
        auth.forget_failure(request, "unlock")
        db.reset_migration_cache()
        try:
            await asyncio.to_thread(_start_up)
        except db.StartupProblem as exc:
            request.app.state.problem, request.app.state.problem_kind = str(exc), exc.kind
            return HTMLResponse(_problem_page(str(exc), True), status_code=503)
        request.app.state.problem = None
        _begin_work(request.app)
        return RedirectResponse("/", status_code=303)

    @app.get("/api/openapi.json", include_in_schema=False)
    def route_map() -> JSONResponse:
        # For someone developing against this server. The code is open, but there's no reason to hand everyone a map.
        if not instance_settings.developer_mode():
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return JSONResponse(app.openapi())

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/privacy", include_in_schema=False)
    async def privacy() -> Response:
        return Response(status_code=307, headers={"Location": "https://health.syntropylabs.io/privacy"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        # Same body as FastAPI's default, except it doesn't send back what was sent: that could be someone's password,
        # or everything they posted.
        errors = [{k: v for k, v in e.items() if k not in ("input", "url")} for e in exc.errors()]
        return JSONResponse({"detail": db.finite(jsonable_encoder(errors))}, status_code=422)

    @app.exception_handler(db.StartupProblem)
    async def startup_problem(request: Request, exc: db.StartupProblem) -> Response:
        return _problem_response(request, str(exc))

    @app.exception_handler(security.ServerBusy)
    async def too_many_password_checks(request: Request, exc: security.ServerBusy) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Cache-Control": "no-store"})

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
