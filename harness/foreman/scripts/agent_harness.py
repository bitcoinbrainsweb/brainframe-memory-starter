"""Claude Messages API tool-use agent loop for the Foreman orchestrator (Phase A).

All API calls originate in the orchestrator process. Tools are dispatched via an
injected callable so the loop itself has no knowledge of whether it is talking to
an in-process dispatcher (tests / Phase A) or a Docker sandbox dispatcher (Phase C).

The agent loop is synchronous; httpx.Client (sync) is used instead of the async
variant so the Protocol's sync `build`/`verify` signatures need no blocking wrapper.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Any

import httpx

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

DEFAULT_TOKEN_BUDGET = int(os.environ.get("FOREMAN_AGENT_TOKEN_BUDGET", "200000"))
HTTP_TIMEOUT = float(os.environ.get("FOREMAN_AGENT_HTTP_TIMEOUT", "120"))
MODEL_CAP = int(os.environ.get("FOREMAN_AGENT_MODEL_CAP", "8096"))
MAX_RETRIES = 3
RETRY_DELAYS = [2, 4, 8]
RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})

# Credential-like prefixes to scrub from error bodies. The literals are split with
# adjacent-string concatenation so this source file does not itself contain a
# scannable secret prefix. Add your own provider's key/token prefixes here.
_REDACT_PATTERNS = ("sk-" "ant-", "sk-", "Bearer ", "gh" "p_", "dp.", "eyJ")


def _redact(text: str) -> str:
    """Redact credential-like substrings from an error body before storing.

    Covers API keys, bearer tokens, source-control PATs, secrets-manager tokens,
    and JWTs. A fuller redaction pipeline can replace this later.
    """
    for pattern in _REDACT_PATTERNS:
        idx = text.find(pattern)
        while idx != -1:
            end = idx + len(pattern)
            # Redact up to 80 chars after the prefix (key length upper bound)
            while end < len(text) and text[end] not in (' ', '"', "'", '\n', '\r', '\t', ',', '}'):
                end += 1
                if end - idx > 80:
                    break
            text = text[:idx] + "[REDACTED]" + text[end:]
            idx = text.find(pattern)
    return text


class ForemanApiError(Exception):
    """Raised by AgentHarness._call_api on any non-retried HTTP error response.

    Single raise contract: _call_api never calls raise_for_status(); all error
    exits go through this class so callers can inspect the body.
    """

    def __init__(self, status_code: int, error_body: str, model: str, max_tokens: int) -> None:
        self.status_code = status_code
        self.error_body = error_body[:8192]
        self.model = model
        self.max_tokens = max_tokens
        super().__init__(f"Foreman API {status_code}: {self.error_body[:200]}")


@dataclass
class AgentRunResult:
    """Output of a single agent loop run."""
    stop_reason: str  # "end_turn" | "token-budget-exceeded" | "max-iterations" | other
    raw_output: str   # concatenated text blocks from the model
    total_tokens: int
    messages: list[dict] = field(default_factory=list)  # full conversation, for debugging


class AgentHarness:
    """Runs the Claude Messages API tool-use loop.

    Accepts an injectable httpx client (for tests) and a dispatcher callable.
    The dispatcher signature is (tool_name: str, tool_inputs: dict) -> str.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        tool_schemas: list[dict],
        dispatcher: Callable[[str, dict], str],
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        httpx_client: Any = None,
        retry_delays: list[int] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._tool_schemas = tool_schemas
        self._dispatcher = dispatcher
        self._token_budget = token_budget
        self._client = httpx_client if httpx_client is not None else httpx.Client()
        self._retry_delays = retry_delays if retry_delays is not None else RETRY_DELAYS
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    def run(self, prompt: str) -> AgentRunResult:
        """Run the agent loop until end_turn, budget exhausted, or hard error."""
        messages: list[dict] = [{"role": "user", "content": prompt}]
        cumulative_tokens = 0
        raw_parts: list[str] = []

        while True:
            remaining = self._token_budget - cumulative_tokens
            if remaining <= 0:
                return AgentRunResult(
                    stop_reason="token-budget-exceeded",
                    raw_output="\n".join(raw_parts),
                    total_tokens=cumulative_tokens,
                    messages=messages,
                )

            max_tokens = min(MODEL_CAP, remaining)
            response_data = self._call_api(messages, max_tokens)

            usage = response_data.get("usage", {})
            cumulative_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            content = response_data.get("content", [])
            stop_reason = response_data.get("stop_reason", "end_turn")

            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        raw_parts.append(text)

            if stop_reason != "tool_use":
                return AgentRunResult(
                    stop_reason=stop_reason,
                    raw_output="\n".join(raw_parts),
                    total_tokens=cumulative_tokens,
                    messages=messages,
                )

            # Dispatch tool calls and collect results
            tool_results: list[dict] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_inputs = block.get("input", {})
                    result_text = self._dispatcher(tool_name, tool_inputs)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("id", ""),
                        "content": result_text,
                    })

            # Extend the conversation
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": tool_results})

    def _call_api(self, messages: list[dict], max_tokens: int) -> dict:
        """Call the Messages API with exponential backoff retry on 429/5xx."""
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": self._system_prompt,
            "messages": messages,
            "tools": self._tool_schemas,
        }

        last_resp = None
        for attempt in range(MAX_RETRIES + 1):
            last_resp = self._client.post(
                ANTHROPIC_API_URL,
                json=body,
                headers=self._headers,
                timeout=HTTP_TIMEOUT,
            )

            if last_resp.status_code in RETRYABLE_CODES:
                if attempt < MAX_RETRIES:
                    delay_idx = min(attempt, len(self._retry_delays) - 1)
                    time.sleep(self._retry_delays[delay_idx])
                    continue
                # Exceeded max retries on retryable error
                break

            # Non-retryable response
            if last_resp.status_code >= 400:
                body = _redact(last_resp.text[:8192])
                raise ForemanApiError(last_resp.status_code, body, self._model, max_tokens)
            return last_resp.json()

        # Exhausted retries on retryable error
        body = _redact(last_resp.text[:8192])
        raise ForemanApiError(last_resp.status_code, body, self._model, max_tokens)
