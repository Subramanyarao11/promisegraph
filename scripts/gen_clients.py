"""Bundle resource + command schemas and generate Python/TypeScript DTOs into contracts/generated/.

Run: python3 scripts/gen_clients.py   (needs uv and npx)
"""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "contracts"
res = json.loads((C / "resources.schema.json").read_text())
cmd = json.loads((C / "commands.schema.json").read_text())
text = json.dumps(cmd).replace('"resources.schema.json#/$defs/', '"#/$defs/')
defs = {**res["$defs"], **json.loads(text)["$defs"]}
bundle = {"$schema": res["$schema"], "title": "PromiseGraphContracts", "type": "object", "$defs": defs}
(C / "bundle.schema.json").write_text(json.dumps(bundle, indent=2) + "\n")

digest = hashlib.sha256()
for name in ("resources.schema.json", "commands.schema.json"):
    digest.update((C / name).read_bytes())
digest.update((ROOT / "fixtures" / "contracts" / "examples.json").read_bytes())
(C / "CONTRACT_HASH").write_text(f"1.0.0 {digest.hexdigest()}\n")

gen = C / "generated"
subprocess.run([
    "uv", "run", "--quiet", "--no-project", "--with", "datamodel-code-generator==0.84.0", "datamodel-codegen",
    "--input", str(C / "bundle.schema.json"), "--input-file-type", "jsonschema",
    "--output", str(gen / "python" / "promisegraph_contracts.py"), "--output-model-type", "pydantic_v2.BaseModel",
    "--use-standard-collections", "--use-union-operator", "--target-python-version", "3.12",
    "--disable-timestamp", "--enum-field-as-literal", "all", "--use-annotated",
], check=True)
ts = subprocess.run([
    "npx", "--yes", "json-schema-to-typescript@15.0.4", "--input", str(C / "bundle.schema.json"),
    "--unreachableDefinitions", "--additionalProperties", "false", "--bannerComment",
    "/* Generated from contracts/bundle.schema.json (contract 1.0.0). Do not edit. */",
], check=True, capture_output=True, text=True).stdout
(gen / "ts" / "contracts.ts").write_text(ts)
print("generated", (C / "CONTRACT_HASH").read_text().strip())
