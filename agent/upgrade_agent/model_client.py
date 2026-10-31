"""One OpenAI-compatible Chat Completions call, and reading tool calls out of its reply.

Works with vLLM, OpenAI and OpenAI-compatible gateways (one in front of
Anthropic included): POST {MODEL_BASE_URL}/chat/completions, tools in the
`tools` parameter. Small models often answer with the tool call written into
the text instead (a JSON object, a ```json fence, or <tool_call> tags), so
`calls_in_text` reads those too. No retries here: one bad call is the loop's
to answer, and the workflow owns retries.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from dataclasses import dataclass, field
from typing import Any

import httpx

from upgrade_agent.config import Settings

log = logging.getLogger(__name__)


class ModelError(Exception):
    """The endpoint could not answer; the message is safe to show (no key, no body)."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: str  # raw JSON text, as the model wrote it
    id: str | None = None  # None when read out of the text


@dataclass
class ModelReply:
    content: str
    tool_calls: list[ToolCall]
    message: dict[str, Any]  # the assistant message, to send back as history
    usage: dict[str, Any] = field(default_factory=dict)


def client(settings: Settings) -> httpx.AsyncClient:
    """One client per invocation (CLAUDE.md §5 of the workflows repo: nothing shared across runs)."""
    headers = {"Content-Type": "application/json"}
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"
    verify: ssl.SSLContext | bool = True
    if settings.model_tls_ca:
        verify = ssl.create_default_context(cafile=settings.model_tls_ca)
    return httpx.AsyncClient(base_url=settings.model_base_url, headers=headers, verify=verify)


async def chat(
    http: httpx.AsyncClient,
    settings: Settings,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> ModelReply:
    body: dict[str, Any] = {
        "model": settings.model_name,
        "messages": messages,
        "temperature": settings.model_temperature,
        "max_tokens": settings.model_max_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    try:
        response = await http.post("/chat/completions", json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ModelError(f"the model endpoint did not answer within {timeout:.0f}s") from None
    except httpx.HTTPError as err:
        raise ModelError(f"the model endpoint is unreachable ({type(err).__name__})") from None
    if response.status_code >= 400:
        raise ModelError(f"the model endpoint answered HTTP {response.status_code}: {_error_text(response)}")
    try:
        payload = response.json()
        message = payload["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError("the model endpoint's reply is not a chat completion") from None
    usage = payload.get("usage") or {}
    log.info("model call: prompt_tokens=%s completion_tokens=%s", usage.get("prompt_tokens"), usage.get("completion_tokens"))
    calls = []
    for raw in message.get("tool_calls") or []:
        function = (raw or {}).get("function") or {}
        if function.get("name"):
            arguments = function.get("arguments")
            calls.append(ToolCall(
                name=str(function["name"]),
                arguments=arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
                id=raw.get("id"),
            ))
    content = message.get("content") or ""
    if not isinstance(content, str):  # some gateways send a list of parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    history = {"role": "assistant", "content": content}
    if message.get("tool_calls"):
        history["tool_calls"] = message["tool_calls"]
    return ModelReply(content=content, tool_calls=calls, message=history, usage=usage)


def _error_text(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
        text = error.get("message") if isinstance(error, dict) else error
    except (ValueError, AttributeError):
        text = response.text
    return str(text or "")[:300]


_THINK = re.compile(r"<think>.*?</think>", re.S)


def calls_in_text(content: str) -> list[ToolCall]:
    """Tool calls a model wrote as text: {"name", "arguments"}, {"tool", "arguments"}, or a bare verdict."""
    text = _THINK.sub("", content or "")
    decoder = json.JSONDecoder()
    calls: list[ToolCall] = []
    index = 0
    while (index := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        index = end
        if not isinstance(value, dict):
            continue
        name = value.get("name") or value.get("tool")
        arguments = value.get("arguments", value.get("parameters"))
        if isinstance(name, str) and isinstance(arguments, (dict, str)):
            calls.append(ToolCall(name=name, arguments=arguments if isinstance(arguments, str) else json.dumps(arguments)))
        elif "verdict" in value and "diagnosis" in value:
            calls.append(ToolCall(name="submit_verdict", arguments=json.dumps(value)))
    return calls
