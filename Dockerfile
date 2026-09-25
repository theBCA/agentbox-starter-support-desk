# python:3.12-slim, NOT alpine: claude-agent-sdk publishes no
# musllinux wheel, so on Alpine pip falls back to an sdist that
# does not bundle the Claude Code CLI the SDK spawns -- it would
# install cleanly and fail at runtime. openai-agents and
# google-adk pull binary wheels (grpc, protobuf, tokenizers)
# that have no musllinux build either and would compile from
# source. Verified on PyPI 2026-08-30.
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The browser console `app/main.py` mounts at `/`. Copied as its own layer
# after the dependency install, so an edit to the page does not re-run pip --
# and copied at all because the mount resolves `ui/` from the source file's
# own directory, not from the working directory: an image that skips this
# still serves the whole API and still answers /health, it just 404s the
# console.
COPY ui ./ui

# The concept: the prompt, the eight steps and their sentences, the sample
# data. Source, not a mount -- it is what this application IS.
COPY concept ./concept

# AgentBox ADOPTS the image's own uid rather than imposing one, and REFUSES an
# image that runs as root (`APP-UID`). An image with no `USER` runs as uid 0,
# so provisioning stops with *the image declares no USER, which means it runs
# as root* -- measured 2026-09-19, when this starter could not be onboarded at
# all. Declaring a uid here is therefore not hardening advice, it is the
# minimum an application must do to be adoptable, and this is the shape to
# copy.
#
# `--create-home` is what puts `/home/agent` in `/etc/passwd`, and that is
# load-bearing twice over: it is the second step of the HOME resolution order
# (`Config.Env`, then `/etc/passwd`, then REFUSE), and it is the directory the
# platform chowns and mounts this application's HOME volume onto. The path
# agrees with `runtime_agentic_files: /home/agent/agent-state` in
# `agentbox-config.yaml`; changing one without the other puts the agent's
# state somewhere neither guard watches.
#
# Everything above this line runs as root on purpose -- pip installs into
# /usr/local, which the app only ever reads.
RUN useradd --create-home --home-dir /home/agent --uid 10001 --user-group agent
USER 10001:10001

EXPOSE 8080

# Confirmed live 2026-08-28: under gVisor (every custom app's sandbox
# runtime), a fresh `python -c "..."` healthcheck process pays real,
# repeated import overhead on EVERY invocation (measured: `import socket`
# alone ~1.4s, `import http.client` another ~2.8s, both near-zero CPU time
# -- pure gVisor syscall-interception wait, not computation, so it doesn't
# improve between runs). A 3s timeout combined with the full
# urllib/http.client/ssl import chain consistently flapped a perfectly
# healthy app to "unhealthy" every ~10s. Using bare `socket` (skips the
# http.client/ssl import chain) and a generous timeout keeps this robust
# against that overhead instead of fighting it -- copy this pattern, not
# the old one, into any app built from this template.
HEALTHCHECK --interval=10s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), timeout=8).close()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
