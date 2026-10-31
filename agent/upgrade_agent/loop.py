"""One invocation: a bounded tool-calling loop that ends in an AgentVerdict.

  1. load the Brain runbooks for the step (AGENT.md #20), recorded as a tool call;
  2. ask the model, run the READ tools it calls, give it the results; repeat;
  3. when it calls submit_verdict, check its proposals against the catalog and
     answer.

Every way this can go wrong ends in a `needs_human` verdict that says why,
never an exception: the model unreachable, a reply with no usable tool call
after the repair prompts, or a budget (tool calls, model calls, wall clock)
spent. A verdict cut short says what the agent had found so far.

The agent never writes. `tier1_enabled` is read and ignored: everything is a
proposal a person approves (AGENT.md #25), until the judge and the tier-1
tools exist (DESIGN §8.3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from upgrade_agent import catalog, model_client, prompt, runbooks
from upgrade_agent.config import Settings
from upgrade_agent.contract import AgentRequest, AgentVerdict, ToolCallRecord
from upgrade_agent.model_client import ModelError, ModelReply, ToolCall
from upgrade_agent.tools import SUBMIT_VERDICT, Toolbox, specs

log = logging.getLogger(__name__)

_RECORD_CHARS = 500  # a tool result as kept in the verdict (and so in workflow history)
_DIAGNOSIS_CHARS = 4000
_MIN_CALL_SECONDS = 5.0  # less wall clock than this left: do not start another model call


class _ProposalArgs(BaseModel):
    kind: str
    target: str
    uid: str = ""
    rationale: str = ""


class _VerdictArgs(BaseModel):
    """What submit_verdict takes; checked before anything is believed."""

    diagnosis: str = Field(min_length=1)
    confidence: Literal["verified", "observed", "hypothesis"]
    verdict: Literal["retry_gate", "needs_human", "abort_recommended"]
    proposals: list[_ProposalArgs] = Field(default_factory=list)


@dataclass
class _Run:
    request: AgentRequest
    deadline: float
    records: list[ToolCallRecord] = field(default_factory=list)
    tool_calls: int = 0
    model_calls: int = 0
    repairs: int = 0
    last_text: str = ""

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def record(self, tool: str, args: dict[str, Any], result: str) -> None:
        self.records.append(ToolCallRecord(
            tool=tool,
            args={k: v if isinstance(v, str) else json.dumps(v) for k, v in args.items()},
            tier=0,
            result=result[:_RECORD_CHARS],
            at=datetime.now(UTC),
        ))

    def cut_short(self, why: str) -> AgentVerdict:
        """needs_human, saying why the agent stopped and what it had found so far."""
        found = [f"{r.tool}({', '.join(f'{k}={v}' for k, v in r.args.items())}) -> "
                 f"{r.result.splitlines()[0][:160] if r.result else ''}" for r in self.records[-10:]]
        diagnosis = f"The agent stopped before a verdict: {why}."
        if self.last_text.strip():
            diagnosis += f"\nIts last notes: {self.last_text.strip()[:1500]}"
        if found:
            diagnosis += "\nWhat it read:\n" + "\n".join(f"- {line}" for line in found)
        return AgentVerdict(diagnosis=diagnosis[:_DIAGNOSIS_CHARS], confidence="hypothesis",
                            actions_taken=self.records, verdict="needs_human")


def _verdict(run: _Run, args: _VerdictArgs) -> AgentVerdict:
    proposals = [catalog.Proposal(kind=p.kind, target=p.target, args={"uid": p.uid} if p.uid else {},
                                  rationale=p.rationale) for p in args.proposals]
    kept, notes = catalog.accept(run.request.invocation_id, proposals)
    verdict = args.verdict
    diagnosis = args.diagnosis.strip()
    if any(note.startswith("dropped") for note in notes):
        # A proposal the workflow could never run means the agent misread its
        # limits; a person should see that rather than the gate re-running.
        verdict = "needs_human" if verdict == "retry_gate" else verdict
    if notes:
        diagnosis += "\n\nChecked by the agent service: " + "; ".join(notes)
    return AgentVerdict(diagnosis=diagnosis[:_DIAGNOSIS_CHARS], confidence=args.confidence,
                        actions_taken=run.records, actions_proposed=kept, verdict=verdict)


def _parse(call: ToolCall) -> dict[str, Any]:
    arguments = json.loads(call.arguments or "{}")
    if not isinstance(arguments, dict):
        raise ValueError("the arguments are not a JSON object")
    return arguments


async def invoke(request: AgentRequest, settings: Settings, *, toolbox: Toolbox | None = None) -> AgentVerdict:
    """The verdict for one request. Never raises for anything the model or a cluster does."""
    budget = request.budget
    run = _Run(request=request, deadline=time.monotonic() + budget.wall_clock_seconds
               - settings.wall_clock_margin_seconds)

    loaded = runbooks.load(settings.runbook_dir, profile=request.profile.value, phase=request.phase,
                           gate=request.gate, version=request.step.version)
    run.record("brain.load_runbooks",
               {"profile": request.profile.value, "phase": request.phase, "gate": request.gate},
               ", ".join(r.name for r in loaded) if loaded else
               ("no runbook directory mounted" if not settings.runbook_dir else "no runbook matched"))

    if not (settings.model_base_url and settings.model_name):
        return run.cut_short("no model is configured (MODEL_BASE_URL and MODEL_NAME)")

    tool_specs = specs(list(request.evidence.sections), catalog.KINDS)
    native = settings.model_tool_mode == "native"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt.system_prompt(None if native else tool_specs)},
        {"role": "user", "content": prompt.request_message(request, loaded)},
    ]
    owned = toolbox is None
    toolbox = toolbox or Toolbox(request, settings)
    async with model_client.client(settings) as http:
        if owned:
            await toolbox.__aenter__()
        try:
            return await _loop(run, settings, http, toolbox, messages, tool_specs if native else None)
        finally:
            if owned:
                await toolbox.__aexit__(None, None, None)


async def _loop(run: _Run, settings: Settings, http: httpx.AsyncClient, toolbox: Toolbox,
                messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> AgentVerdict:
    budget = run.request.budget
    while True:
        if run.model_calls >= budget.max_model_calls:
            return run.cut_short(f"its budget of {budget.max_model_calls} model calls is spent")
        if run.left() < _MIN_CALL_SECONDS:
            return run.cut_short(f"its wall clock of {budget.wall_clock_seconds} s is spent")
        run.model_calls += 1
        try:
            reply: ModelReply = await model_client.chat(
                http, settings, messages, tools, timeout=min(settings.model_timeout_seconds, run.left()))
        except ModelError as err:
            return run.cut_short(str(err))
        if reply.content.strip():
            run.last_text = reply.content
        calls = reply.tool_calls or model_client.calls_in_text(reply.content)
        if not calls:
            if run.repairs >= settings.repair_attempts:
                return run.cut_short(f"the model gave no tool call or verdict, after {run.repairs} repair "
                                     f"prompts (last reply: {reply.content.strip()[:300]!r})")
            run.repairs += 1
            messages += [reply.message, {"role": "user", "content": prompt.REPAIR}]
            continue

        native_ids = all(call.id for call in calls) and bool(reply.tool_calls)
        messages.append(reply.message if native_ids else {"role": "assistant", "content": reply.content})
        results: list[tuple[ToolCall, str]] = []
        malformed = False
        for call in calls:
            try:
                args = _parse(call)
            except ValueError as err:
                malformed = True
                results.append((call, f"error: the arguments are not valid JSON ({err})"))
                continue
            if call.name == SUBMIT_VERDICT:
                try:
                    return _verdict(run, _VerdictArgs.model_validate(args))
                except ValidationError as err:
                    malformed = True
                    errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors()[:5])
                    results.append((call, f"error: submit_verdict refused: {errors}"))
                    continue
            if run.tool_calls >= budget.max_tool_calls:
                return run.cut_short(f"its budget of {budget.max_tool_calls} tool calls is spent")
            run.tool_calls += 1
            try:
                result = await asyncio.wait_for(toolbox.call(call.name, args), timeout=max(1.0, run.left()))
            except TimeoutError:
                return run.cut_short(f"its wall clock of {budget.wall_clock_seconds} s ran out during {call.name}")
            run.record(call.name, args, result)
            results.append((call, result))
        if malformed:
            if run.repairs >= settings.repair_attempts:
                return run.cut_short(f"the model's tool calls stayed malformed after {run.repairs} repair prompts "
                                     f"({results[-1][1][:300]})")
            run.repairs += 1
        if native_ids:
            messages += [{"role": "tool", "tool_call_id": call.id, "content": prompt.tool_result(call.name, result)}
                         for call, result in results]
        else:
            messages.append({"role": "user", "content": "\n\n".join(
                f"Result of {call.name}:\n{prompt.tool_result(call.name, result)}" for call, result in results)})
