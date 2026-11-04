"""One in-memory job per user: analyze -> choose -> download -> package.

Everything is keyed by user name, so a user can never reach someone else's
job. A restart loses running jobs (acceptable for a handful of friends), which
is why the jobs folder is wiped on startup.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.backend.downloads.adapter import AuthError, DeezloadAdapter, ItemUpdate
from app.backend.downloads.layout import collection_dir, plan_downloads, safe_name, write_m3u8
from app.backend.history import History
from app.backend.matching.cache import MatchCache
from app.backend.matching.collection import match_collection
from app.backend.matching.matcher import DeezerMatcher
from app.backend.models import DeezerTrack, MatchResult, MatchStatus, SpotifyCollection
from app.backend.settings import Settings
from app.backend.spotify.resolver import SpotifyError, SpotifyResolver, parse_url
from app.backend.users import UserStore

log = logging.getLogger(__name__)

BUSY_PHASES = ("downloading", "packaging")
CLEANUP_EVERY_S = 600


class Busy(Exception):
    """A download is running for this user."""


class NoArl(Exception):
    """Neither the user's own ARL nor DEFAULT_ARL is set."""


class NoJob(Exception):
    """The user has no job."""


class WrongPhase(Exception):
    """The job is not in a phase that allows this action."""


class DiskFull(Exception):
    """Free disk space is below MIN_FREE_GB."""


@dataclass
class Job:
    id: str
    owner: str
    dir: Path
    created: float = field(default_factory=time.time)
    phase: str = "analyzing"
    error: str | None = None
    collection: SpotifyCollection | None = None
    results: list[MatchResult | None] = field(default_factory=list)
    skipped: list[bool] = field(default_factory=list)
    downloads: list[dict] = field(default_factory=list)
    analysis_done: int = 0
    account: dict | None = None
    file: Path | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


def _new_download() -> dict:
    return {"state": "pending", "progress": 0, "quality": None, "error": None}


def _deezer(t: DeezerTrack) -> dict:
    return {"id": t.id, "title": t.title, "artist": t.artist, "album": t.album, "duration_s": t.duration_s}


class JobManager:
    def __init__(self, settings: Settings, users: UserStore, adapter: DeezloadAdapter,
                 resolver: SpotifyResolver, matcher: DeezerMatcher, cache: MatchCache,
                 history: History | None = None):
        self.settings = settings
        self.history = history or History(Path(settings.data_dir) / "history.json")
        self.users = users
        self.adapter = adapter
        self.resolver = resolver
        self.matcher = matcher
        self.cache = cache
        self.jobs_dir = Path(settings.data_dir) / "jobs"
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        shutil.rmtree(self.jobs_dir, ignore_errors=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    # --- actions -------------------------------------------------------------

    def analyze(self, user: str, url: str) -> str:
        kind, sid = parse_url(url)  # SpotifyError goes straight to the caller
        with self._lock:
            old = self._jobs.get(user)
            if old and old.phase in BUSY_PHASES:
                raise Busy("Já há um download em andamento.")
            job_id = secrets.token_hex(6)
            job = Job(job_id, user, self.jobs_dir / job_id)
            self._jobs[user] = job
        if old:
            old.cancel.set()
            shutil.rmtree(old.dir, ignore_errors=True)
        threading.Thread(target=self._analyze, args=(job, f"spotify:{kind}:{sid}"),
                         name=f"analyze-{job_id}", daemon=True).start()
        return job_id

    def choose(self, user: str, index: int, deezer_id: int | None) -> None:
        with self._lock:
            job = self._job(user)
            if job.phase != "ready":
                raise WrongPhase("Só dá para escolher antes do download.")
            if not 0 <= index < len(job.results):
                raise ValueError("Track inexistente.")
            if deezer_id is None:
                job.skipped[index] = True
                return
            result = job.results[index]
            options = [(result.track, result.score)] if result and result.track else []
            options += result.alternatives if result else []
            picked = next(((t, s) for t, s in options if t.id == deezer_id), None)
            if picked is None:
                raise ValueError("Esse id do Deezer não é uma das opções desta track.")
            track = picked[0]
            job.results[index] = MatchResult(MatchStatus.MATCHED, track, picked[1], "manual",
                                             result.alternatives)
            job.skipped[index] = False
            spotify_id = job.collection.tracks[index].spotify_id
        self.cache.put_manual(spotify_id, track)

    def download(self, user: str) -> None:
        with self._lock:
            job = self._job(user)
            if job.phase != "ready":
                raise WrongPhase("O job não está pronto para download.")
        arl = self.users.arl_for(user, self.settings.default_arl)
        if not arl:
            raise NoArl("Nenhum ARL configurado: salve o seu ou peça para configurarem o DEFAULT_ARL.")
        data_dir = Path(self.settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(data_dir).free / 1e9
        if free_gb < self.settings.min_free_gb:
            raise DiskFull(f"Pouco espaço livre no servidor ({free_gb:.1f} GB). Tente mais tarde.")
        prefs = self.users.get(user)
        own = self.users.has_own_arl(user)
        # Fixed by the server (not a user choice): tuned once for the VM in .env.
        concurrency = self.settings.max_user_concurrency

        with self._lock:
            if self._jobs.get(user) is not job or job.phase != "ready":
                raise WrongPhase("O job mudou; tente de novo.")
            results = [MatchResult(MatchStatus.NOT_FOUND) if skip else r
                       for r, skip in zip(job.results, job.skipped)]
            planned = plan_downloads(job.collection, results, str(job.dir / "files"))
            if not planned:
                raise ValueError("Nenhuma track para baixar.")
            job.phase = "downloading"
            job.account = {"max_quality": None, "using_own_arl": own}
        threading.Thread(target=self._download, args=(job, arl, own, prefs.quality, concurrency, planned),
                         name=f"download-{job.id}", daemon=True).start()

    def cancel(self, user: str) -> None:
        with self._lock:
            job = self._job(user)
            job.cancel.set()
            # A running download ends as "cancelled" from its own thread.
            if job.phase in ("analyzing", "ready"):
                job.phase = "cancelled"

    def discard(self, user: str) -> None:
        """Forget the user's job and its files (the "Limpar" button). No job is fine."""
        with self._lock:
            job = self._jobs.get(user)
            if not job:
                return
            if job.phase in BUSY_PHASES:
                raise Busy("Há um download em andamento. Cancele antes de limpar.")
            del self._jobs[user]
        job.cancel.set()
        shutil.rmtree(job.dir, ignore_errors=True)

    # --- reads ---------------------------------------------------------------

    def snapshot(self, user: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(user)
            return self._snapshot(job) if job else None

    def file_for(self, user: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(user)
            if not job or job.phase != "done" or not job.file:
                return None
            path = job.file
        # Never serve anything outside this job's folder.
        if not path.resolve().is_relative_to(job.dir.resolve()) or not path.is_file():
            return None
        return path

    # --- cleanup -------------------------------------------------------------

    def start_cleanup(self) -> None:
        def loop():
            while True:
                time.sleep(CLEANUP_EVERY_S)
                try:
                    self.cleanup_expired()
                except Exception:
                    log.exception("job cleanup failed")
        threading.Thread(target=loop, name="job-cleanup", daemon=True).start()

    def cleanup_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        ttl = self.settings.job_ttl_hours * 3600
        with self._lock:
            expired = [j for j in self._jobs.values()
                       if now - j.created > ttl and j.phase not in BUSY_PHASES]
            for job in expired:
                del self._jobs[job.owner]
                job.cancel.set()
        for job in expired:
            shutil.rmtree(job.dir, ignore_errors=True)

    # --- worker threads ------------------------------------------------------

    def _analyze(self, job: Job, uri: str) -> None:
        started = time.monotonic()
        try:
            collection = self.resolver.resolve(uri)
            with self._lock:
                job.collection = collection
                n = len(collection.tracks)
                job.results, job.skipped = [None] * n, [False] * n
                job.downloads = [_new_download() for _ in range(n)]

            def progress(done: int, total: int) -> None:
                with self._lock:
                    job.analysis_done = done

            results = match_collection(collection, self.matcher, self.cache, on_progress=progress)
            with self._lock:
                job.results = results
                job.analysis_done = len(results)
                if job.phase == "analyzing":
                    job.phase = "ready"
            matched = sum(r.status is MatchStatus.MATCHED for r in results)
            log.info("job %s (%s): analysed %s %r, %d/%d matched in %.0fs", job.id, job.owner,
                     collection.kind, collection.title, matched, len(results), time.monotonic() - started)
        except SpotifyError as e:
            self._fail(job, str(e))
        except Exception:
            log.exception("analysis of job %s failed", job.id)
            self._fail(job, "Erro inesperado ao analisar o link.")

    def _download(self, job: Job, arl: str, own: bool, quality: str, concurrency: int,
                  planned: list) -> None:
        which = "seu ARL próprio" if own else "o ARL padrão do servidor"
        indices = [i for i, _ in planned]
        paths: list[str | None] = [None] * len(job.downloads)
        started = time.monotonic()
        log.info("job %s (%s): downloading %d tracks, %s, %d at a time",
                 job.id, job.owner, len(planned), quality, concurrency)

        def on_update(u: ItemUpdate) -> None:
            i = indices[u.index]
            with self._lock:
                d = job.downloads[i]
                d["state"], d["progress"] = u.state, u.progress
                if u.quality:
                    d["quality"] = u.quality
                d["error"] = u.error
                if u.path:
                    paths[i] = u.path

        try:
            account = self.adapter.account_info(arl)
            with self._lock:
                job.account["max_quality"] = account.max_quality
                for i in indices:
                    job.downloads[i]["state"] = "queued"
            self.adapter.run(arl, quality, concurrency, [item for _, item in planned], on_update, job.cancel)
            if job.cancel.is_set():
                shutil.rmtree(job.dir / "files", ignore_errors=True)
                with self._lock:
                    job.phase = "cancelled"
                log.info("job %s (%s): cancelled after %.0fs", job.id, job.owner, time.monotonic() - started)
                return
            with self._lock:
                job.phase = "packaging"
            write_m3u8(job.collection, paths)
            done = [p for p in paths if p]
            if not done:
                self._fail(job, "Nenhuma track foi baixada.")
                return
            if len(done) == 1:
                file = Path(done[0])
            else:
                file = self._zip(job)
                # Only the zip is served, so the loose tracks would just double the disk use until the TTL.
                shutil.rmtree(job.dir / "files", ignore_errors=True)
            try:
                with self._lock:
                    got = [d["quality"] for d in job.downloads if d["state"] == "done" and d["quality"]]
                # The format most of the tracks really came in (a job can mix FLAC and MP3).
                self.history.add(job.owner, job.collection, max(set(got), key=got.count) if got else None)
            except Exception:  # the shared list is a nicety; never lose a finished download over it
                log.exception("could not record job %s in the history", job.id)
            with self._lock:
                job.file = file
                job.phase = "done"
            log.info("job %s (%s): done, %d/%d tracks, %.0f MB in %.0fs", job.id, job.owner, len(done),
                     len(planned), file.stat().st_size / 1e6, time.monotonic() - started)
        except AuthError:
            self._fail(job, f"ARL inválido ou expirado ({which}).")
        except Exception:
            log.exception("download of job %s failed", job.id)
            self._fail(job, "Erro inesperado durante o download.")

    def _zip(self, job: Job) -> Path:
        folder = Path(collection_dir(job.collection, str(job.dir / "files")))
        target = job.dir / f"{safe_name(job.collection.title)}.zip"
        # FLAC/MP3 are already compressed, so storing is fast and costs almost nothing.
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as z:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(folder).as_posix())
        return target

    def _fail(self, job: Job, message: str) -> None:
        with self._lock:
            job.phase = "error"
            job.error = message
        log.warning("job %s (%s): %s", job.id, job.owner, message)

    # --- helpers (caller holds the lock) --------------------------------------

    def _job(self, user: str) -> Job:
        job = self._jobs.get(user)
        if not job:
            raise NoJob("Nenhum job.")
        return job

    def _snapshot(self, job: Job) -> dict:
        c = job.collection
        items, totals = [], dict.fromkeys(
            ["matched", "ambiguous", "not_found", "skipped",
             "queued", "downloading", "done", "failed", "cancelled"], 0)
        for i, t in enumerate(c.tracks if c else ()):
            r, skipped, dl = job.results[i], job.skipped[i], job.downloads[i]
            match = None
            if r:
                match = {"status": r.status.value, "score": round(r.score, 3), "method": r.method,
                         "deezer": _deezer(r.track) if r.track else None,
                         "alternatives": [{"deezer": _deezer(d), "score": round(s, 3)}
                                          for d, s in r.alternatives]}
                if skipped:
                    totals["skipped"] += 1
                else:
                    totals[r.status.value.lower()] += 1
            if dl["state"] in totals:
                totals[dl["state"]] += 1
            items.append({"index": i, "title": t.title, "artists": list(t.artists),
                          "duration_ms": t.duration_ms, "skipped": skipped,
                          "match": match, "download": dict(dl)})
        return {
            "job_id": job.id,
            "phase": job.phase,
            "error": job.error,
            "collection": c and {"kind": c.kind, "title": c.title, "subtitle": c.subtitle,
                                 "cover_url": c.cover_url, "track_count": len(c.tracks),
                                 "truncated": c.truncated},
            "analysis": {"done": job.analysis_done, "total": len(c.tracks) if c else 0},
            "account": dict(job.account) if job.account else None,
            "items": items,
            "totals": totals,
            "download_url": "/api/job/file" if job.phase == "done" else None,
        }
