"""Brain runbooks for one invocation (AGENT.md #20), from a mounted directory of markdown.

The first step of every invocation loads the profile's runbook and the pages
tagged for this step, so the agent starts from what the team already knows.
The directory is optional (RUNBOOK_DIR: a ConfigMap, or a checkout of the
Brain repo); without it the agent diagnoses from the evidence alone.

What a runbook says is reference material, shown to the model as DATA. It
cannot widen what the agent may propose: that is catalog.py, and the
workflow's own catalog behind it (AGENT.md #23).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_LIMIT_CHARS = 24000
_TAGS = {"upgrade", "hcp-upgrade"}


@dataclass(frozen=True)
class Runbook:
    name: str  # path relative to the directory
    text: str


def _front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _as_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {str(v).lower() for v in value}
    return set()


def load(directory: str, *, profile: str, phase: str, gate: str, version: str) -> list[Runbook]:
    """The pages for this step, the profile's runbook first, at most _LIMIT_CHARS in all.

    Picked: `upgrade-<profile>.md` (DESIGN §10: runbooks/hypershift/upgrade-<profile>.md),
    then every page whose `tags` name the profile, the phase, the gate or
    "upgrade", unless its `applies_to` names OCP minors and not this step's.
    """
    root = Path(directory) if directory else None
    if root is None or not root.is_dir():
        return []
    minor = ".".join(version.split(".")[:2])
    wanted = {profile.lower(), phase.lower(), gate.lower()} | _TAGS
    first: list[tuple[int, Runbook]] = []
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        name = str(path.relative_to(root))
        if path.stem == f"upgrade-{profile}":
            first.append((0, Runbook(name, text)))
            continue
        meta = _front_matter(text)
        if not (_as_set(meta.get("tags")) & wanted):
            continue
        applies = {a for a in _as_set(meta.get("applies_to")) if a.startswith("ocp-")}
        if applies and f"ocp-{minor}" not in applies:
            continue
        first.append((1, Runbook(name, text)))
    chosen, used = [], 0
    for _, runbook in sorted(first, key=lambda item: item[0]):
        if used + len(runbook.text) > _LIMIT_CHARS:
            continue
        chosen.append(runbook)
        used += len(runbook.text)
    return chosen
