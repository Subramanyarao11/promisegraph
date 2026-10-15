"""Contract validator shared by all builders (coordinator-owned).

Usage (from repo root):
  uv run --with jsonschema==4.25.1 python contracts/tools/validate.py            # self-check schemas + examples
  from contracts.tools.validate import validate; validate("Request", obj)        # raises on mismatch
Definitions are looked up first in resources.schema.json, then commands.schema.json.
"""

import json
import sys
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parent.parent
RES_URI = "https://promisegraph.local/contracts/resources.schema.json"
CMD_URI = "https://promisegraph.local/contracts/commands.schema.json"


@lru_cache(maxsize=1)
def _load():
    res = json.loads((CONTRACTS / "resources.schema.json").read_text())
    cmd = json.loads((CONTRACTS / "commands.schema.json").read_text())
    reg = Registry().with_resources([(RES_URI, Resource.from_contents(res)), (CMD_URI, Resource.from_contents(cmd))])
    return res, cmd, reg


@lru_cache(maxsize=256)
def validator(name: str) -> Draft202012Validator:
    res, cmd, reg = _load()
    base = RES_URI if name in res["$defs"] else CMD_URI
    if name not in res["$defs"] and name not in cmd["$defs"]:
        raise KeyError(f"unknown contract definition {name}")
    return Draft202012Validator({"$ref": f"{base}#/$defs/{name}"}, registry=reg)


def errors(name: str, value) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator(name).iter_errors(value)]


def validate(name: str, value) -> None:
    errs = errors(name, value)
    if errs:
        raise ValueError(f"{name} contract violation: " + "; ".join(errs[:10]))


def _self_check() -> int:
    res, cmd, _ = _load()
    Draft202012Validator.check_schema(res)
    Draft202012Validator.check_schema(cmd)
    ex = json.loads((CONTRACTS.parent / "fixtures" / "contracts" / "examples.json").read_text())
    failures = 0
    for i, e in enumerate(ex["resource_examples"]):
        for err in errors(e["schema"], e["data"]):
            failures += 1
            print(f"resource_examples[{i}] {e['schema']}: {err}")
    for e in ex["endpoint_examples"]:
        body = e["body"]
        if "next_after_seq" in body:
            errs = errors("EventsPage", body)
        else:
            errs = errors("Meta", body["meta"])
        for err in errs:
            failures += 1
            print(f"{e['method']} {e['path']}: {err}")
    print(f"schemas ok; {len(ex['resource_examples'])} resource examples, {len(ex['endpoint_examples'])} endpoint examples; {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_self_check())
