# spot2deez: FastAPI app + vendored Deezload. Multi-arch base (the server is ARM64);
# every dependency in requirements.txt ships aarch64/x86_64 wheels, so no compiler is needed.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged runtime user. The entrypoint starts as root only to fix the
# ownership of the /data bind mount, then drops to this user with setpriv.
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --create-home --home-dir /home/app app \
 && command -v setpriv >/dev/null

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY vendor/ vendor/
# vendor/deezload is a git submodule: an empty folder means it was not cloned.
RUN test -f vendor/deezload/deezload.py || { \
      echo "ERROR: vendor/deezload/deezload.py not found." >&2; \
      echo "Clone with --recursive or run: git submodule update --init" >&2; \
      exit 1; }

COPY app/ app/
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
# The checkout may have CRLF line endings (Windows); the shebang would break.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
 && chmod 755 /usr/local/bin/entrypoint.sh \
 && mkdir -p /data && chown app:app /data

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.backend.main"]
