# anvilkit-parser

The parser Job image of the AnvilKit Agent platform ([DD-07 §2](https://github.com/ancyloce/anvilkit-services/blob/main/docs/architecture/knowledge.md), delivery.md P15): the fixed Docling parser and its trusted stager. Knowledge's parser launcher runs it in the `anvilkit-parsing` namespace from a reviewed profile of `contracts/jobs/profiles.json` (`parser-docling-dev-v1`, DEVELOPMENT_ONLY; `parser-docling-v1`, gVisor, DISABLED until qualified).

## Layout of one Job

- `stage-in` (init, UID 10002, `anvilkit-stage in`): downloads exactly the envelope's input through the one presigned GET URL of the Job's stage Secret, stops at the declared size, verifies the SHA-256 digest and writes it read-only to `/stage/in/source`.
- `parse` (init, UID 10001, `anvilkit-parse`, the AF_UNIX-only syscall profile, no URL mounted): validates the envelope and limits against the contract schema, checks the bytes and media type, bounds DOCX archives (entries, expanded size, per-member and total ratio, path escapes, links, encryption, actual expansion) and PDFs (password, page count), converts with Docling 2.130.0 (pinned layout weights `docling-project/docling-layout-heron@8f39ad3`, Apache-2.0; OCR, table structure, enrichment, remote services, external plugins and every resource fetch disabled; `HF_HUB_OFFLINE=1`) or plain text with a line-preserving splitter, chunks with Docling's hierarchical chunker and writes `/stage/out/result.json` (`#/$defs/parseResult`). Every chunk carries its locator (element, heading path, page, line range) and quality flags; text that reads like an instruction to a model is flagged `instruction_like` and stays data. A refused document is the verdict `rejected` with its failure code (exit 0); a non-zero exit is an infrastructure failure.
- `stage-out` (UID 10002, `anvilkit-stage out`): uploads the bounded result of this launch through the one presigned PUT URL and reports digest, size and verdict in the termination message — a claim only: Knowledge reads and verifies the uploaded bytes itself.

The stagers run `python -I -B -u -m anvilkit_parser.stage` (`bin/anvilkit-stage`, P0.7): isolated mode ignores every `PYTHON*` variable, the user site and the current and script directories, so only the image's read-only, root-owned directories are on the trusted stager's import path (the package reaches it through a `.pth` file in the venv); the working directory is `/opt/anvilkit`, and the launcher gives the stagers and the parser separate `/tmp` (HOME) volumes. `stage-out` reads `result.json` without following a link and only as a regular file, and neither stager follows a redirect. The image is built on Debian 13 (trixie) with the fixed versions of its CRITICAL findings pinned: the release scan reports none.

## Inputs

`requirements.lock` is the hash-pinned dependency set (`uv pip compile --generate-hashes`, CPU torch 2.14.0), installed with `--require-hashes`; `models.lock` pins the layout weights by revision and per-file SHA-256, fetched once at build (`bin/fetch-models.py`); the contract schema comes from the contracts repository (named build context).

## Checks

`python -m unittest discover -s tests` with the locked environment, `ANVILKIT_JOB_SCHEMA` and `DOCLING_ARTIFACTS_PATH`, or inside the image: `docker run --rm --network none --read-only --tmpfs /tmp --entrypoint /opt/venv/bin/python -w /opt/anvilkit anvilkit-parser:dev -m unittest discover -s tests`. Build: `docker build --build-context contracts=<contracts checkout> -t anvilkit-parser:dev .` (`../../contracts` in the parent checkout). Build and publish to the development registry from the parent checkout: `sh deploy/dev/images.sh --parser` (a rebuilt image is a new digest and needs a profile revision and the policy digest).

## Repository

This Job image is the repository `anvilkit-job-parser` (the image and the Python package keep the name `anvilkit-parser`), mounted in the parent `anvilkit-services` as the submodule `jobs/parser`. It keeps no contract definitions of its own: `jobs/job.schema.json` comes from the contracts repository as the image's named `contracts` build context. CI (`.github/workflows/ci.yml`) checks out that repository at the commit the parent pins (`1c3ef95`, tag `go/v0.1.10`), builds the image and runs the tests inside it with no network.
