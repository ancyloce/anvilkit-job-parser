# syntax=docker/dockerfile:1
# anvilkit-parser: the fixed Docling parser Job image (DD-07 §2, P15).
# Build from this directory with the contracts as a named context:
#   docker build --build-context contracts=../../contracts -t anvilkit-parser:dev .
# Dependencies are installed from the hash-locked requirements.lock; the
# layout weights are fetched once here at the revision and SHA-256 sums of
# models.lock. At run time the image downloads nothing (offline flags, no
# plugins, no remote services) and runs as UID 10001 on a read-only root.
# P0.7: Debian 13 (trixie): every CRITICAL finding of its base has a fixed
# version (bookworm's sqlite, perl-base and zlib findings had none), pinned
# in the final stage below.
ARG PYTHON_IMAGE=python:3.12.12-slim-trixie@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock \
      --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu
COPY models.lock bin/fetch-models.py /tmp/
RUN /opt/venv/bin/python /tmp/fetch-models.py /tmp/models.lock /opt/docling-models
COPY src /opt/anvilkit/src
# The package reaches every interpreter through the venv's site-packages, so
# the stager's isolated mode (-I ignores PYTHONPATH) still imports it from the
# read-only, root-owned /opt/anvilkit/src.
RUN printf '/opt/anvilkit/src\n' > /opt/venv/lib/python3.12/site-packages/anvilkit-parser.pth
# The parser's own sources must compile; third-party packages ship some
# non-Python-3.12 templates and fixtures, so their precompilation (an
# optimization for the read-only root) tolerates those files.
RUN /opt/venv/bin/python -m compileall -q /opt/anvilkit/src \
 && { /opt/venv/bin/python -m compileall -q /opt/venv >/dev/null 2>&1 || true; }

FROM ${PYTHON_IMAGE}
# Debian security fixes the base digest predates (CRITICAL with a fixed
# version: CVE-2026-31789 OpenSSL, CVE-2026-13221/42496/8376 perl-base; HIGH
# CVE-2026-41992 gzip, CVE-2026-86145/89157/89161/103111 libpcre2,
# CVE-2026-11822/11824 SQLite, CVE-2026-4878 libcap2,
# CVE-2026-53612/53613/53614/53615 util-linux), pinned to the fixed versions;
# the build stage and its weights are unaffected.
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade libssl3t64=3.5.7-1~deb13u3 openssl=3.5.7-1~deb13u3 \
      openssl-provider-legacy=3.5.7-1~deb13u3 perl-base=5.40.1-6+deb13u1 \
      gzip=1.13-1+deb13u1 libpcre2-8-0=10.46-1~deb13u3 libsqlite3-0=3.46.1-7+deb13u2 libcap2=1:2.75-10+deb13u1+b3 \
      bsdutils=1:2.41.5-0+deb13u1 libblkid1=2.41.5-0+deb13u1 liblastlog2-2=2.41.5-0+deb13u1 libmount1=2.41.5-0+deb13u1 \
      libsmartcols1=2.41.5-0+deb13u1 libuuid1=2.41.5-0+deb13u1 login=1:4.16.0-2+really2.41.5-0+deb13u1 \
      mount=2.41.5-0+deb13u1 util-linux=2.41.5-0+deb13u1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/docling-models /opt/docling-models
COPY --from=build /opt/anvilkit/src /opt/anvilkit/src
COPY --from=contracts jobs/job.schema.json /opt/anvilkit/job.schema.json
COPY models.lock /opt/anvilkit/models.lock
COPY tests /opt/anvilkit/tests
COPY bin/anvilkit-parse bin/anvilkit-stage /usr/local/bin/
ENV PYTHONSAFEPATH=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models \
    ANVILKIT_JOB_SCHEMA=/opt/anvilkit/job.schema.json \
    HOME=/tmp
USER 10001:10001
# Read-only and root-owned: never on any interpreter's path (P0.7). HOME and
# /tmp are each container's own emptyDir (the launcher mounts one for the
# stagers and another for the parser).
WORKDIR /opt/anvilkit
ENTRYPOINT ["/usr/local/bin/anvilkit-parse"]
