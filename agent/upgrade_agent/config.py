"""Configuration, from the environment only (the chart's ConfigMap and Secrets).

The model endpoint is any OpenAI-compatible Chat Completions API: vLLM, OpenAI,
or a gateway in front of another provider. Nothing here names a host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    # --- the model ------------------------------------------------------------
    # Base URL up to and including the API version, e.g. http://<vllm>:8000/v1;
    # the client appends /chat/completions.
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = ""  # from a Secret; empty for an endpoint without auth
    model_tls_ca: str = ""  # a PEM file path, for an endpoint signed by an internal CA
    model_timeout_seconds: float = 120.0  # one chat completion; capped by the wall clock left
    model_max_tokens: int = 2048
    model_temperature: float = 0.0
    # "native": tools go in the request's `tools` parameter (needs a server with
    # tool calling on, e.g. vLLM --enable-auto-tool-choice). "prompt": the tools
    # are described in the system prompt and the model answers with JSON.
    model_tool_mode: str = "native"
    # Prompts sent back after a reply with no tool call or bad JSON, per invocation.
    repair_attempts: int = 2

    # --- the service ----------------------------------------------------------
    agent_api_token: str = ""  # when set, POST /v1/invoke needs "Authorization: Bearer <it>"
    state_dir: str = ""  # verdicts kept here (the Sandbox's volume) as well as in memory
    runbook_dir: str = ""  # mounted Brain runbooks (markdown); skipped when absent
    max_concurrent: int = 4
    # Answer this long before the request's wall clock runs out.
    wall_clock_margin_seconds: int = 30
    tool_result_chars: int = 8000  # what one tool result shows the model

    # --- the clusters ---------------------------------------------------------
    # One sub-directory per hosted cluster, each holding the agent's OWN
    # credential for it: `kubeconfig`, or `token` (+ `ca.crt`).
    hosted_credentials_dir: str = "/var/run/upgrade-agent/hosted"
    # The hosted API server as seen from the MCE's pod network; {hcp_namespace}
    # is filled in. Empty: use the kubeconfig's own server.
    hosted_api_server: str = "https://kube-apiserver.{hcp_namespace}.svc:6443"
    mce_token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    mce_ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    kube_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        settings = cls(
            model_base_url=env("MODEL_BASE_URL", "").strip().rstrip("/"),
            model_name=env("MODEL_NAME", "").strip(),
            model_api_key=env("MODEL_API_KEY", "").strip(),
            model_tls_ca=env("MODEL_TLS_CA", "").strip(),
            model_timeout_seconds=_float("MODEL_TIMEOUT_SECONDS", cls.model_timeout_seconds),
            model_max_tokens=_int("MODEL_MAX_TOKENS", cls.model_max_tokens),
            model_temperature=_float("MODEL_TEMPERATURE", cls.model_temperature),
            model_tool_mode=env("MODEL_TOOL_MODE", cls.model_tool_mode).strip() or cls.model_tool_mode,
            repair_attempts=_int("REPAIR_ATTEMPTS", cls.repair_attempts),
            agent_api_token=env("AGENT_API_TOKEN", "").strip(),
            state_dir=env("STATE_DIR", "").strip(),
            runbook_dir=env("RUNBOOK_DIR", "").strip(),
            max_concurrent=_int("MAX_CONCURRENT", cls.max_concurrent),
            wall_clock_margin_seconds=_int("WALL_CLOCK_MARGIN_SECONDS", cls.wall_clock_margin_seconds),
            tool_result_chars=_int("TOOL_RESULT_CHARS", cls.tool_result_chars),
            hosted_credentials_dir=env("HOSTED_CREDENTIALS_DIR", cls.hosted_credentials_dir),
            hosted_api_server=env("HOSTED_API_SERVER", cls.hosted_api_server).strip(),
            mce_token_file=env("MCE_TOKEN_FILE", cls.mce_token_file),
            mce_ca_file=env("MCE_CA_FILE", cls.mce_ca_file),
            kube_timeout_seconds=_float("KUBE_TIMEOUT_SECONDS", cls.kube_timeout_seconds),
        )
        if settings.model_tool_mode not in ("native", "prompt"):
            raise ValueError(f"MODEL_TOOL_MODE must be native or prompt (got {settings.model_tool_mode!r})")
        if settings.model_base_url and not settings.model_base_url.startswith(("http://", "https://")):
            raise ValueError("MODEL_BASE_URL must be an http:// or https:// URL")
        return settings
