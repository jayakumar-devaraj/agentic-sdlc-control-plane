# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# git is needed at RUNTIME here, not only at build time: every run clones its own
# target repository. That is the difference from this platform's other images,
# where git is a build-only dependency and gets removed again.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# agentic-events comes from agentic-sdlc-eventbus, whose visibility changes. This
# build works either way, and that is the requirement (ADR-0015).
#
# **The secret is optional by design.** Mounted and non-empty -> git is configured to
# use it, so a private eventbus resolves. Absent or empty -> the install falls through
# to anonymous HTTPS, so a public eventbus resolves with no credential involved at all.
# Neither case needs this file edited, which is what "works public or private" means.
#
# **The credential never lands in the image.** A BuildKit secret exists only under
# /run/secrets for the life of this RUN and is never written to a layer; the git config
# it creates is removed inside the *same* layer. `Assert the built image carries no
# credential` in CI checks /root/.gitconfig is 0 bytes and is the regression guard for
# exactly this - it was deliberately kept when 080ffae removed the earlier version of
# this block, so that restoring one could be verified rather than trusted.
#
# The RUNTIME PAT is a separate concern and is unchanged: every run clones its own
# target repository, reading the token from GIT_PAT_FILE via a credential helper
# invoked at request time. That is why git is installed above.
RUN --mount=type=secret,id=github_pat \
    sh -c ' \
        if [ -s /run/secrets/github_pat ]; then \
            git config --global url."https://$(cat /run/secrets/github_pat)@github.com/".insteadOf "https://github.com/"; \
        fi && \
        pip install --no-cache-dir -r requirements.txt; \
        status=$?; \
        rm -f /root/.gitconfig; \
        exit $status \
    '

COPY . .

# Runs clone into here; also holds the audit JSONL. Mounted as a volume in compose
# so a restart does not orphan a parked run's workspace.
RUN mkdir -p /workspaces

# Empty by design. Replay-mode fixtures describe work on one specific service, so
# this platform ships none - mount your own here to enable replay mode. See
# docs/adr/0001.
RUN mkdir -p /fixtures

CMD ["python", "-m", "agentic_control_plane.main"]
