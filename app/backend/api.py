"""HTTP API (FastAPI): thin routes over JobManager and UserStore.

Who the user is comes from X-Remote-User, set by Caddy after basic auth (the
app port is never published, so the header is trusted). Mutating requests need
X-Requested-With: spot2deez, which a third-party site cannot send without a
CORS preflight that we never answer. Errors are always {"error": "<safe text>"}.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backend import secrets_filter
from app.backend.jobs import Busy, DiskFull, JobManager, NoArl, NoJob, WrongPhase
from app.backend.settings import ROOT, Settings, get_settings
from app.backend.spotify.resolver import SpotifyError
from app.backend.users import UserStore, valid_username

FRONTEND_DIR = ROOT / "app" / "frontend"
CSRF_HEADER, CSRF_VALUE = "x-requested-with", "spot2deez"
MUTATING = ("POST", "PUT", "PATCH", "DELETE")

ERRORS: list[tuple[type[Exception], int]] = [
    (SpotifyError, 400), (Busy, 409), (NoArl, 400), (DiskFull, 400),
    (NoJob, 404), (WrongPhase, 409), (ValueError, 400),
]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class _Frontend(StaticFiles):
    """StaticFiles that answers 404 (not a 500) while app/frontend/ does not exist yet."""

    async def check_config(self) -> None:
        if self.directory is not None and not Path(self.directory).is_dir():
            raise StarletteHTTPException(404, "Frontend não encontrado.")
        await super().check_config()


def _build_jobs(settings: Settings, users: UserStore) -> JobManager:
    # Imported here so tests that inject a JobManager never touch the real clients.
    from app.backend.deezer.api import DeezerPublicApi
    from app.backend.downloads.adapter import DeezloadAdapter
    from app.backend.matching.cache import MatchCache
    from app.backend.matching.matcher import DeezerMatcher
    from app.backend.spotify.resolver import SpotifyResolver

    return JobManager(settings, users, DeezloadAdapter(settings.max_parallel_downloads),
                      SpotifyResolver(), DeezerMatcher(DeezerPublicApi()),
                      MatchCache(Path(settings.data_dir) / "match_cache.json"))


def create_app(settings: Settings | None = None, users: UserStore | None = None,
               jobs: JobManager | None = None, frontend_dir: Path | None = None) -> FastAPI:
    """Build the app. Anything not injected is built from get_settings() (ConfigError propagates)."""
    settings = settings or get_settings()
    if users is None:
        users = jobs.users if jobs else UserStore(Path(settings.data_dir) / "users", settings.secret_key,
                                                  settings.max_user_concurrency)
    jobs = jobs or _build_jobs(settings, users)
    frontend_dir = Path(frontend_dir or FRONTEND_DIR)

    # ARLs that must never reach a log: the default plus any received via PUT /api/me.
    secrets: set[str] = {settings.default_arl} if settings.default_arl else set()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Logging is configured by now (main.py), so the filter reaches the root handlers.
        f = secrets_filter.install(lambda: list(secrets))
        jobs.start_cleanup()
        try:
            yield
        finally:
            secrets_filter.uninstall(f)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.users, app.state.jobs, app.state.secrets = settings, users, jobs, secrets

    # --- middleware: identity + CSRF -----------------------------------------

    @app.middleware("http")
    async def identity_and_csrf(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        user = (request.headers.get("x-remote-user") or "").strip()
        if not user and settings.app_env == "dev" and settings.dev_user:
            user = settings.dev_user
        if not user:
            return error(401, "Não autenticado.")
        user = user.lower()
        if not valid_username(user):
            return error(400, "Nome de usuário inválido.")
        if request.method in MUTATING and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return error(403, "Requisição recusada (falta X-Requested-With).")
        request.state.user = user
        return await call_next(request)

    # --- errors --------------------------------------------------------------

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return error(e.status, str(e))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, e: StarletteHTTPException):
        return error(e.status_code, str(e.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, e: RequestValidationError):
        return error(400, "Requisição inválida.")

    def _domain_handler(status: int):
        async def handler(request: Request, e: Exception):
            return error(status, str(e) or "Requisição inválida.")
        return handler

    # Looked up by the exception's MRO, so ValueError subclasses land on 400 too.
    for cls, status in ERRORS:
        app.add_exception_handler(cls, _domain_handler(status))

    @app.exception_handler(Exception)
    async def _any_error(request: Request, e: Exception):
        # Starlette re-raises after this, so the server still logs the traceback (filtered).
        return error(500, "Erro interno.")

    # --- helpers -------------------------------------------------------------

    async def body(request: Request) -> dict:
        raw = await request.body()
        if not raw.strip():
            return {}
        try:
            data = await request.json()
        except ValueError:
            raise ApiError(400, "Corpo JSON inválido.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "O corpo deve ser um objeto JSON.")
        return data

    def me(user: str) -> dict:
        prefs = users.get(user)
        return {"user": user, "quality": prefs.quality, "concurrency": prefs.concurrency,
                "own_arl": users.has_own_arl(user),
                "default_arl_configured": bool(settings.default_arl),
                "max_concurrency": settings.max_user_concurrency}

    def snapshot(user: str) -> dict:
        snap = jobs.snapshot(user)
        if snap is None:
            raise NoJob("Nenhum job.")
        return snap

    # --- routes --------------------------------------------------------------

    @app.get("/api/me")
    async def get_me(request: Request):
        return me(request.state.user)

    @app.put("/api/me")
    async def put_me(request: Request):
        user, data = request.state.user, await body(request)
        if "arl" in data and data["arl"] is not None and not isinstance(data["arl"], str):
            raise ApiError(400, "arl deve ser texto ou null.")
        if data.get("quality") is not None or data.get("concurrency") is not None:
            users.update(user, quality=data.get("quality"), concurrency=data.get("concurrency"))
        if "arl" in data:
            arl = (data["arl"] or "").strip()
            if arl:
                secrets.add(arl)  # before it can show up anywhere near a log
            users.set_arl(user, arl or None)
        return me(user)

    @app.post("/api/analyze", status_code=202)
    async def analyze(request: Request):
        url = (await body(request)).get("url")
        if not isinstance(url, str) or not url.strip():
            raise ApiError(400, "Informe a URL do Spotify.")
        # parse_url may follow a spotify.link redirect, so keep it off the event loop.
        job_id = await run_in_threadpool(jobs.analyze, request.state.user, url.strip())
        return {"job_id": job_id}

    @app.get("/api/job")
    async def get_job(request: Request):
        snap = jobs.snapshot(request.state.user)
        return snap if snap is not None else Response(status_code=204)

    @app.post("/api/job/items/{index}/choose")
    async def choose(index: int, request: Request):
        data = await body(request)
        if "deezer_id" not in data:
            raise ApiError(400, "Informe deezer_id (ou null para pular).")
        deezer_id = data["deezer_id"]
        if deezer_id is not None and (isinstance(deezer_id, bool) or not isinstance(deezer_id, int)):
            raise ApiError(400, "deezer_id deve ser um número ou null.")
        jobs.choose(request.state.user, index, deezer_id)
        return snapshot(request.state.user)

    @app.post("/api/job/download")
    async def download(request: Request):
        await body(request)
        jobs.download(request.state.user)
        return snapshot(request.state.user)

    @app.post("/api/job/cancel")
    async def cancel(request: Request):
        await body(request)
        jobs.cancel(request.state.user)
        return snapshot(request.state.user)

    @app.get("/api/job/file")
    async def job_file(request: Request):
        path = jobs.file_for(request.state.user)
        if path is None:
            raise ApiError(404, "Nenhum arquivo pronto.")
        return FileResponse(path, filename=path.name, content_disposition_type="attachment")

    # --- frontend ------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        page = frontend_dir / "index.html"
        if not page.is_file():
            return error(404, "Frontend ainda não instalado (app/frontend/index.html).")
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    app.mount("/", _Frontend(directory=frontend_dir, check_dir=False), name="frontend")
    return app
