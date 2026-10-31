"""Idempotency on `invocation_id`: a retry gets the verdict the first attempt reached.

The workflow retries the agent activity once (a dead worker), with the same
invocation id. The retry must not run the agent a second time: in memory, a
finished verdict is returned and a run still in flight is joined; with
STATE_DIR set (the Sandbox's volume), a verdict also survives a pod restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path

from upgrade_agent.contract import AgentVerdict

log = logging.getLogger(__name__)

_KEEP_IN_MEMORY = 500


class VerdictStore:
    def __init__(self, state_dir: str = ""):
        self._dir = Path(state_dir) if state_dir else None
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
        self._done: OrderedDict[str, AgentVerdict] = OrderedDict()
        self._running: dict[str, asyncio.Task[AgentVerdict]] = {}

    def _path(self, invocation_id: str) -> Path | None:
        if self._dir is None:
            return None
        return self._dir / f"{hashlib.sha256(invocation_id.encode()).hexdigest()}.json"

    def get(self, invocation_id: str) -> AgentVerdict | None:
        if invocation_id in self._done:
            return self._done[invocation_id]
        path = self._path(invocation_id)
        if path is not None and path.is_file():
            try:
                return AgentVerdict.model_validate_json(path.read_text())
            except ValueError:
                log.warning("unreadable stored verdict %s; running again", path.name)
        return None

    def _keep(self, invocation_id: str, verdict: AgentVerdict) -> None:
        self._done[invocation_id] = verdict
        while len(self._done) > _KEEP_IN_MEMORY:
            self._done.popitem(last=False)
        path = self._path(invocation_id)
        if path is not None:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(verdict.model_dump(mode="json")))
            os.replace(tmp, path)

    async def get_or_run(self, invocation_id: str, run: Callable[[], Awaitable[AgentVerdict]]) -> AgentVerdict:
        stored = self.get(invocation_id)
        if stored is not None:
            return stored
        task = self._running.get(invocation_id)
        if task is None:
            task = asyncio.create_task(self._run(invocation_id, run))
            self._running[invocation_id] = task
        # A caller that goes away (the activity was cancelled) does not cancel
        # the run: a retry joins it instead of starting another.
        return await asyncio.shield(task)

    async def _run(self, invocation_id: str, run: Callable[[], Awaitable[AgentVerdict]]) -> AgentVerdict:
        try:
            verdict = await run()
            self._keep(invocation_id, verdict)
            return verdict
        finally:
            self._running.pop(invocation_id, None)
