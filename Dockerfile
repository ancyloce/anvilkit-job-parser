# syntax=docker/dockerfile:1
# anvilkit-parser: the fixed Docling parser Job image (DD-07 §2, P15).
# Build from this directory with the contracts as a named context:
#   docker build --build-context contracts=../../contracts -t anvilkit-parser:dev .
# Dependencies are installed from the hash-locked requirements.lock; the
# layout weights are fetched once here at the revision and SHA-256 sums of
# models.lock. At run time the image downloads nothing (offline flags, no
# plugins, no remote services) and runs as UID 10001 on a read-only root.
ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock \
      --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu
COPY models.lock bin/fetch-models.py /tmp/
RUN /opt/venv/bin/python /tmp/fetch-models.py /tmp/models.lock /opt/docling-models
COPY src /opt/anvilkit/src
# The parser's own sources must compile; third-party packages ship some
# non-Python-3.12 templates and fixtures, so their precompilation (an
# optimization for the read-only root) tolerates those files.
RUN /opt/venv/bin/python -m compileall -q /opt/anvilkit/src \
 && { /opt/venv/bin/python -m compileall -q /opt/venv >/dev/null 2>&1 || true; }

FROM ${PYTHON_IMAGE}
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/docling-models /opt/docling-models
COPY --from=build /opt/anvilkit/src /opt/anvilkit/src
COPY --from=contracts jobs/job.schema.json /opt/anvilkit/job.schema.json
COPY models.lock /opt/anvilkit/models.lock
COPY tests /opt/anvilkit/tests
COPY bin/anvilkit-parse bin/anvilkit-stage /usr/local/bin/
ENV PYTHONPATH=/opt/anvilkit/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DOCLING_ARTIFACTS_PATH=/opt/docling-models \
    ANVILKIT_JOB_SCHEMA=/opt/anvilkit/job.schema.json \
    HOME=/tmp
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/anvilkit-parse"]
