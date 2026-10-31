"""The upgrade agent's HTTP API: POST /v1/invoke, AgentRequest in, AgentVerdict out.

Called by the workflows repo's `upgrade-agent-worker` (activity
`invoke_upgrade_agent`), once per failed gate. A request the contract cannot
decode is a 422; everything that goes wrong while the agent works is a
`needs_human` verdict, so the caller can tell "my request was wrong" from
"the agent could not decide".

Run: uvicorn upgrade_agent.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import Depends, FastAPI, Header, HTTPException

from upgrade_agent import loop
from upgrade_agent.config import Settings
from upgrade_agent.contract import AgentRequest, AgentVerdict
from upgrade_agent.store import VerdictStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("upgrade_agent")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = VerdictStore(settings.state_dir)
    slots = asyncio.Semaphore(max(1, settings.max_concurrent))
    api = FastAPI(title="upgrade-agent", version="1",
                  description="Diagnoses one failed gate of an HCP upgrade. Reads only; proposes, never acts.")

    def authorized(authorization: str = Header(default="")) -> None:
        if settings.agent_api_token and not hmac.compare_digest(
                authorization.encode(), f"Bearer {settings.agent_api_token}".encode()):
            raise HTTPException(status_code=401, detail="missing or wrong bearer token")

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/readyz")
    async def readyz() -> dict[str, object]:
        # What it would talk to, never a credential.
        return {"model": settings.model_name or None, "model_configured": bool(settings.model_base_url),
                "tool_mode": settings.model_tool_mode, "runbooks": bool(settings.runbook_dir)}

    @api.post("/v1/invoke", response_model=AgentVerdict, dependencies=[Depends(authorized)])
    async def invoke(request: AgentRequest) -> AgentVerdict:
        async def run() -> AgentVerdict:
            async with slots:
                log.info("invocation %s: %s %s gate %s", request.invocation_id, request.cluster, request.phase,
                         request.gate)
                try:
                    verdict = await loop.invoke(request, settings)
                except Exception as err:  # a bug here must still answer: a person, not a retry loop
                    log.exception("invocation %s failed", request.invocation_id)
                    verdict = AgentVerdict(diagnosis=f"the agent service failed: {type(err).__name__}",
                                           confidence="hypothesis", verdict="needs_human")
                log.info("invocation %s: %s (%d reads, %d proposals)", request.invocation_id, verdict.verdict,
                         len(verdict.actions_taken), len(verdict.actions_proposed))
                return verdict

        return await store.get_or_run(request.invocation_id, run)

    return api


app = create_app()
