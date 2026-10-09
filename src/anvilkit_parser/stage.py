"""anvilkit-stage: the trusted stager of a parser Job (DD-07 §2).

`in` runs as an init container before the parser: it downloads exactly the
envelope's input through the one presigned GET URL mounted for it, stops
at the declared size, verifies the SHA-256 digest and writes the bytes
read-only for the parser. `out` runs after the parser: it reads the result
within the profile's output bound, checks it names this launch, uploads it
through the one presigned PUT URL and reports digest, size and verdict in
the termination message. The URLs are read from files only these
containers mount and are never printed; proxies from the environment are
ignored and a redirect is an error (a presigned URL is never followed
elsewhere). The result is read without following a link and only as a
regular file (the parser container writes that directory). Knowledge
re-reads and verifies the uploaded bytes itself: the termination message is
a claim, never evidence.

P0.7: the stager runs as `python -I -B -u -m anvilkit_parser.stage`
(bin/anvilkit-stage): isolated mode (no PYTHON* environment, no user site,
no current or script directory on sys.path), so only the image's read-only,
root-owned directories are on its import path; the package reaches it
through a .pth file in the venv.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import urllib.request
from pathlib import Path

from . import contract

TIMEOUT_SECONDS = 60


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx answer ends the transfer (urllib raises HTTPError): the object store
    answers a presigned URL directly, so a redirect is never legitimate."""

    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class StageError(Exception):
    pass


def _url(path: Path) -> str:
    url = path.read_text(encoding="utf-8").strip()
    if not url.startswith(("http://", "https://")):
        raise StageError("stage URL is not HTTP(S)")
    return url


def stage_in(envelope: contract.Envelope, url_file: Path, target: Path) -> None:
    expected = int(envelope.input["sizeBytes"])
    digest = hashlib.sha256()
    received = 0
    with _opener.open(urllib.request.Request(_url(url_file), method="GET"), timeout=TIMEOUT_SECONDS) as resp:
        tmp = target.with_suffix(".partial")
        with tmp.open("wb") as out:
            while chunk := resp.read(65536):
                received += len(chunk)
                if received > expected:
                    raise StageError("input exceeds its declared size")
                digest.update(chunk)
                out.write(chunk)
    if received != expected or "sha256:" + digest.hexdigest() != envelope.input["digest"]:
        tmp.unlink(missing_ok=True)
        raise StageError("input differs from the envelope digest or size")
    tmp.chmod(0o444)
    tmp.rename(target)


def stage_out(envelope: contract.Envelope, limits: contract.Limits, url_file: Path, source: Path) -> dict[str, str]:
    # The parser container owns /stage/out: no link is followed and only a regular
    # file is read (a FIFO would block, a link could point at the URL files).
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as err:
        raise StageError("result is not a readable regular file") from err
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise StageError("result is not a regular file")
        if st.st_size > limits.maxOutputBytes:
            raise StageError("result exceeds the output bound")
        data = f.read(limits.maxOutputBytes + 1)
    if len(data) > limits.maxOutputBytes:
        raise StageError("result exceeds the output bound")
    result = json.loads(data)
    if not isinstance(result, dict) or result.get("launchKey") != envelope.launch_key:
        raise StageError("result does not name this launch")
    req = urllib.request.Request(
        _url(url_file),
        data=data,
        method="PUT",
        headers={"Content-Type": "application/json", "Content-Length": str(len(data))},
    )
    with _opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
        if resp.status not in (200, 201, 204):
            raise StageError(f"upload answered {resp.status}")
    return {
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "sizeBytes": str(len(data)),
        "verdict": str(result.get("verdict", "")),
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv not in (["in"], ["out"]):
        sys.stderr.write("usage: anvilkit-stage in|out\n")
        return 2
    try:
        envelope, limits = contract.load(contract.Schema())
        if argv == ["in"]:
            stage_in(envelope, contract.GET_URL_FILE, contract.INPUT_PATH)
            sys.stderr.write("stage in: verified\n")
        else:
            report = stage_out(envelope, limits, contract.PUT_URL_FILE, contract.RESULT_PATH)
            contract.TERMINATION_LOG.write_text(json.dumps(report, separators=(",", ":")), encoding="utf-8")
            sys.stderr.write(f"stage out: {report['verdict']} {report['sizeBytes']} bytes\n")
    except (contract.ContractError, StageError, OSError, ValueError) as err:
        # The message never contains a URL: errors are classified, not echoed.
        kind = type(err).__name__
        detail = str(err) if isinstance(err, (StageError, contract.ContractError)) else ""
        sys.stderr.write(f"stage {argv[0]} failed: {kind} {detail}\n")
        try:
            contract.TERMINATION_LOG.write_text(json.dumps({"stage": argv[0], "failure": kind}), encoding="utf-8")
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
