"""The parser Job's contract boundary (contracts/jobs/job.schema.json).

The Knowledge launcher passes the parse envelope and the reviewed profile's
parser limits as environment JSON; both are validated against the contract
schema before anything reads the document. The result document is built
here and validated against the same schema before it is written.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_ID = "urn:anvilkit:jobs:v1"
ENVELOPE_ENV = "ANVILKIT_PARSE_ENVELOPE"
LIMITS_ENV = "ANVILKIT_PARSER_LIMITS"
SCHEMA_ENV = "ANVILKIT_JOB_SCHEMA"
DEFAULT_SCHEMA = "/opt/anvilkit/job.schema.json"

INPUT_PATH = Path("/stage/in/source")
RESULT_PATH = Path("/stage/out/result.json")
GET_URL_FILE = Path("/run/anvilkit/stage/get")
PUT_URL_FILE = Path("/run/anvilkit/stage/put")
TERMINATION_LOG = Path("/dev/termination-log")


class ContractError(Exception):
    """The envelope, limits or result do not satisfy the contract."""


def _registry(path: str) -> Registry:
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    if schema.get("$id") != SCHEMA_ID:
        raise ContractError(f"{path} is not {SCHEMA_ID}")
    return Registry().with_resource(SCHEMA_ID, Resource.from_contents(schema))


class Schema:
    def __init__(self, path: str | None = None) -> None:
        self._registry = _registry(path or os.environ.get(SCHEMA_ENV, DEFAULT_SCHEMA))

    def validate(self, ref: str, instance: Any) -> None:
        validator = Draft202012Validator({"$ref": f"{SCHEMA_ID}{ref}"}, registry=self._registry)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            raise ContractError(f"{ref}: {'/'.join(map(str, e.path))}: {e.message[:200]}")


@dataclass(frozen=True)
class Envelope:
    raw: dict[str, Any]

    @property
    def launch_key(self) -> str:
        return self.raw["launchKey"]

    @property
    def input(self) -> dict[str, str]:
        return self.raw["input"]


@dataclass(frozen=True)
class Limits:
    raw: dict[str, Any]

    def __getattr__(self, name: str) -> Any:  # camelCase contract fields
        try:
            return self.raw[name]
        except KeyError as err:
            raise AttributeError(name) from err


def load(schema: Schema, env: dict[str, str] | None = None) -> tuple[Envelope, Limits]:
    env = os.environ if env is None else env
    try:
        envelope = json.loads(env[ENVELOPE_ENV])
        limits = json.loads(env[LIMITS_ENV])
    except (KeyError, json.JSONDecodeError) as err:
        raise ContractError(f"envelope or limits missing or not JSON: {err}") from err
    schema.validate("#/$defs/parseEnvelope", envelope)
    schema.validate("#/$defs/parserLimits", limits)
    return Envelope(envelope), Limits(limits)
