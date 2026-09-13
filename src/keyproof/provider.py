"""Schema-only model access; model output never becomes a command or file path."""

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import TypeVar

import httpx
import weave
from pydantic import BaseModel, ValidationError

from .contracts import RunConfig, Usage
from .telemetry import trace_agent_inputs

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    pass


class BudgetExhausted(ProviderError):
    pass


def strict_schema(model: type[BaseModel]) -> dict:
    """Both providers require every object property in the required list."""
    schema = model.model_json_schema()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            node.pop("default", None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


class ModelClient:
    timeout_seconds = 180

    def __init__(self, config: RunConfig):
        self.config = config
        self.model = config.model or os.environ.get("KEYPROOF_MODEL")
        if config.provider == "openai":
            self.model = self.model or os.environ.get("OPENAI_MODEL")
        self.usage = Usage(input_tokens=0, output_tokens=0, cached_input_tokens=0, complete=False)
        self._uncertain_usage = False
        self._busy = False

    @property
    def identity(self) -> dict:
        return {
            "provider": self.config.provider,
            "model": self.model,
            "model_identity": self.model or "Codex CLI default; exact model not reported",
            "limits": {
                "max_model_calls": self.config.max_model_calls,
                "max_input_tokens": self.config.max_input_tokens,
                "max_output_tokens": self.config.max_output_tokens,
                "token_scope": "aggregate across this run",
                "codex_token_caps": "observed after each call, not hard per-call enforcement",
                "unknown_usage": "stop further calls rather than assume zero",
                "process_timeout_seconds": self.timeout_seconds,
            },
        }

    def check_budget(self) -> None:
        if self._uncertain_usage:
            raise BudgetExhausted("Provider token usage is unknown; further calls are disabled.")
        if self.usage.calls >= self.config.max_model_calls:
            raise BudgetExhausted("Model call budget exhausted.")
        if (self.usage.input_tokens or 0) >= self.config.max_input_tokens:
            raise BudgetExhausted("Aggregate input-token budget exhausted.")
        if (self.usage.output_tokens or 0) >= self.config.max_output_tokens:
            raise BudgetExhausted("Aggregate output-token budget exhausted.")

    def _account(self, usage: dict | None) -> None:
        for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
            value = usage.get(field) if isinstance(usage, dict) else None
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                setattr(self.usage, field, None)
                if field != "cached_input_tokens":
                    self._uncertain_usage = True
            elif getattr(self.usage, field) is not None:
                setattr(self.usage, field, getattr(self.usage, field) + value)

    @weave.op(postprocess_inputs=trace_agent_inputs)
    async def complete(self, prompt: str, response: type[T]) -> T:
        if self._busy:
            raise ProviderError("Concurrent calls cannot share a run budget.")
        self.check_budget()
        if self.config.provider == "codex" and not shutil.which("codex"):
            raise ProviderError("Codex CLI is not installed or is not on PATH.")
        if self.config.provider == "openai" and (
            not self.model
            or not (os.environ.get("KEYPROOF_API_KEY") or os.environ.get("OPENAI_API_KEY"))
        ):
            raise ProviderError(
                "OpenAI-compatible provider requires an API key and an explicit model."
            )
        self._reported_usage = None
        self._busy = True
        self.usage.calls += 1
        started = time.monotonic()
        accounted = False
        try:
            schema = strict_schema(response)
            if self.config.provider == "codex":
                text, usage = await self._codex(prompt, schema)
            else:
                text, usage = await self._openai(prompt, schema)
            self._account(usage)
            accounted = True
            if self._uncertain_usage:
                raise BudgetExhausted(
                    "Response usage is unknown; no unmetered proposal will be used."
                )
            if (
                self.usage.input_tokens > self.config.max_input_tokens
                or self.usage.output_tokens > self.config.max_output_tokens
            ):
                raise BudgetExhausted(
                    "Response exceeded an aggregate token ceiling; proposal discarded."
                )
            try:
                return response.model_validate_json(text)
            except (ValidationError, ValueError) as exc:
                raise ProviderError(
                    "Model response did not satisfy the requested JSON schema."
                ) from exc
        finally:
            if not accounted:
                self._account(self._reported_usage)
            self.usage.elapsed_ms += (time.monotonic() - started) * 1000
            self._busy = False

    async def _codex(self, prompt: str, schema: dict) -> tuple[str, dict | None]:
        executable = shutil.which("codex")
        if not executable:
            raise ProviderError("Codex CLI is not installed or is not on PATH.")
        with tempfile.TemporaryDirectory(prefix="keyproof-model-") as temporary:
            schema_path = Path(temporary) / "response.schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            argv = [
                executable,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--json",
                "-c",
                "features.shell_tool=false",
                "-c",
                'web_search="disabled"',
                "-c",
                'model_reasoning_effort="low"',
                "--output-schema",
                str(schema_path),
            ]
            if self.model:
                argv.extend(["--model", self.model])
            argv.append("-")
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=temporary,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode()), timeout=self.timeout_seconds
                )
            except BaseException:
                # Descendants must not survive cancellation, including CLI worker processes.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                raise
            if process.returncode:
                # CLI diagnostics can contain account details; do not persist raw stderr.
                raise ProviderError(
                    f"Codex exited with status {process.returncode}; no valid completion."
                )
            messages: list[str] = []
            usage = None
            completed = False
            if len(stdout) > 8 * 1024 * 1024:
                raise ProviderError("Codex response exceeded the 8 MiB protocol limit.")
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise ProviderError("Codex emitted invalid JSONL.") from exc
                if not isinstance(event, dict):
                    raise ProviderError("Codex emitted a non-object JSONL event.")
                kind = event.get("type")
                if kind in {"item.started", "item.updated", "item.completed"}:
                    item = event.get("item", {})
                    if not isinstance(item, dict):
                        raise ProviderError("Codex emitted an invalid item.")
                    if item.get("type") not in {"agent_message", "reasoning"}:
                        raise ProviderError(
                            "Codex attempted an unexpected tool or non-message action."
                        )
                    if kind == "item.completed" and item.get("type") == "agent_message":
                        messages.append(item.get("text", ""))
                elif kind == "turn.completed":
                    if completed:
                        raise ProviderError("Codex emitted more than one completion turn.")
                    completed = True
                    usage = event.get("usage")
                    self._reported_usage = usage
                elif kind in {"error", "turn.failed"}:
                    raise ProviderError("Codex reported a failed model turn.")
                elif kind not in {"thread.started", "turn.started"}:
                    raise ProviderError("Codex emitted an unknown protocol event.")
            if not completed or not messages:
                raise ProviderError("Codex did not return a completed JSON response.")
            return messages[-1], usage

    async def _openai(self, prompt: str, schema: dict) -> tuple[str, dict | None]:
        key = os.environ.get("KEYPROOF_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key or not self.model:
            raise ProviderError(
                "OpenAI-compatible provider requires an API key and an explicit model."
            )
        base = os.environ.get("KEYPROOF_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        remaining = self.config.max_output_tokens - (self.usage.output_tokens or 0)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            try:
                result = await client.post(
                    base + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_completion_tokens": remaining,
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "keyproof_response",
                                "strict": True,
                                "schema": schema,
                            },
                        },
                    },
                )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    "OpenAI-compatible request failed at the transport layer."
                ) from exc
        if result.status_code != 200:
            raise ProviderError(f"OpenAI-compatible provider returned HTTP {result.status_code}.")
        try:
            body = result.json()
            raw = body.get("usage") or {}
            usage = {
                "input_tokens": raw.get("prompt_tokens"),
                "output_tokens": raw.get("completion_tokens"),
                "cached_input_tokens": (raw.get("prompt_tokens_details") or {}).get(
                    "cached_tokens"
                ),
            }
            self._reported_usage = usage
            choice = body["choices"][0]
            message = choice["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ProviderError("Provider returned an unexpected tool call.")
            if choice.get("finish_reason") != "stop" or message.get("refusal"):
                raise ProviderError("Provider refused or truncated the requested completion.")
            text = message["content"]
            if not isinstance(text, str):
                raise ValueError("No textual JSON response")
            return text, usage
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Provider returned an invalid completion envelope.") from exc
