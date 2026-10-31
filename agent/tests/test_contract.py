"""The vendored contract still renders the snapshot both repos keep (contract.schema.json).

The workflows repo checks its ORIGINAL models against the same snapshot. If
this fails, the models here no longer match the snapshot; if the workflows
repo's test fails, the originals changed. Either way: copy the classes from
the workflows repo unchanged, then regenerate with
`UPDATE_CONTRACT=1 pytest tests/test_contract.py` and copy the snapshot over.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from upgrade_agent.contract import contract_schema

SNAPSHOT = Path(__file__).resolve().parent.parent / "upgrade_agent" / "contract.schema.json"


def test_the_vendored_models_match_the_snapshot():
    rendered = json.dumps(contract_schema(), indent=1, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_CONTRACT") == "1":
        SNAPSHOT.write_text(rendered)
    assert SNAPSHOT.read_text() == rendered, "the vendored contract drifted from contract.schema.json"
