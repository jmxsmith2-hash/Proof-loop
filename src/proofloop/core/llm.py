"""Gateway for model calls: role routing, caching, usage and cost tracking.

The default heuristic engine never comes through here. This gateway serves the
optional model engines (Anthropic, Ollama) and the scripted ``FakeLLM`` used in tests.
All calls return JSON validated against the schema given in the request.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from proofloop.core.config import Settings
from proofloop.core.cost import call_cost, image_tokens, text_tokens
from proofloop.core.db import Database, utcnow
from proofloop.core.logging import get_logger

log = get_logger(__name__)


class LLMError(RuntimeError):
    """A model call failed or returned output that does not match its schema."""


@dataclass
class LLMRequest:
    role: str
    system: str
    prompt: str
    schema: dict[str, Any]
    images: list[bytes] = field(default_factory=list)  # PNG bytes
    image_size: tuple[int, int] = (1280, 800)
    max_tokens: int = 2000
    seed: int = 0          # part of the cache key, so repeated runs stay independent
    cacheable: bool = True
    model: str | None = None  # per-persona override
    engine: str | None = None  # per-persona override (e.g. one persona on Claude, the rest heuristic)
    cache_system: bool = False  # the system text repeats across calls (e.g. every step of a session): cache it
    effort: str | None = None   # filled in from models.yaml (effort per role) when not set

    def cache_key(self, engine: str, model: str) -> str:
        h = hashlib.sha256()
        for part in (engine, model, self.role, self.system, self.prompt,
                     json.dumps(self.schema, sort_keys=True), str(self.seed), str(self.max_tokens),
                     str(self.effort)):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        for img in self.images:
            h.update(hashlib.sha256(img).digest())
        return h.hexdigest()


@dataclass
class LLMResponse:
    data: dict[str, Any]
    engine: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    cache_read_tokens: int = 0    # input served from the provider's prompt cache (billed at ~0.1x)
    cache_write_tokens: int = 0   # input written to the prompt cache (billed at ~1.25x)
    price_factor: float = 1.0     # 0.5 for batch requests


class LLMClient(Protocol):
    engine: str

    async def complete(self, request: LLMRequest, model: str) -> LLMResponse: ...


# --------------------------------------------------------------------- validation


def validate_json(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Minimal JSON-schema check: type, required, enum, properties, items, min/max."""
    errors: list[str] = []
    expected = schema.get("type")
    type_map: dict[str, Any] = {"object": dict, "array": list, "string": str, "integer": int,
                "number": (int, float), "boolean": bool, "null": type(None)}
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        ok = any(isinstance(data, type_map[t]) and not (t in ("integer", "number") and isinstance(data, bool))
                 for t in types)
        if not ok:
            return [f"{path}: expected {expected}, got {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {data!r} not in {schema['enum']}")
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: {data} < {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: {data} > {schema['maximum']}")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}: missing {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                errors.extend(validate_json(data[key], sub, f"{path}.{key}"))
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            errors.extend(validate_json(item, schema["items"], f"{path}[{i}]"))
    return errors


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Add additionalProperties: false to every object, as structured outputs require."""
    out = dict(schema)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        out["properties"] = {k: _strict_schema(v) for k, v in out.get("properties", {}).items()}
    if "items" in out:
        out["items"] = _strict_schema(out["items"])
    return out


# ------------------------------------------------------------------------ clients


class AnthropicClient:
    """Claude via the official SDK, using JSON-schema structured output."""

    engine = "anthropic"

    def __init__(self) -> None:
        try:
            import anthropic  # noqa: F401  (optional dependency)
        except ImportError as exc:
            raise LLMError("the anthropic engine needs `pip install proofloop[anthropic]`") from exc
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            raise LLMError("ANTHROPIC_API_KEY is not set; use the free heuristic engine or set the key")
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic()

    # Thinking is on for current models and counts towards max_tokens; output is billed as used, so a low cap
    # only risks a truncated (invalid) answer.
    MIN_MAX_TOKENS = 8000

    def _params(self, request: LLMRequest, model: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": _media_type(img),
                                         "data": base64.standard_b64encode(img).decode("ascii")}}
            for img in request.images
        ]
        content.append({"type": "text", "text": request.prompt})
        system: str | list[dict[str, Any]] = request.system
        if request.cache_system:
            system = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": _strict_schema(request.schema)}}
        if request.effort:
            output_config["effort"] = request.effort
        return {"model": model, "max_tokens": max(request.max_tokens, self.MIN_MAX_TOKENS), "system": system,
                "messages": [{"role": "user", "content": content}], "output_config": output_config}

    def _parse(self, response: Any, model: str) -> LLMResponse:
        if response.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("the model ran out of output tokens before finishing its answer")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {text[:200]}") from exc
        usage = response.usage
        return LLMResponse(data=data, engine=self.engine, model=model,
                           input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                           cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                           cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0))

    async def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        a = self._anthropic
        try:
            response = await self._client.messages.create(**self._params(request, model))
        except a.RateLimitError as exc:
            raise LLMError(f"rate limited by Anthropic: {exc}") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise LLMError(f"could not reach Anthropic: {exc}") from exc
        return self._parse(response, model)


class AnthropicBatchClient(AnthropicClient):
    """Claude through the Message Batches API: half price, but results take minutes (up to 24 h).

    Calls that arrive within `window` seconds of each other go into one batch, so a verification step that
    asks many questions at once (one review per issue, one questionnaire per session) waits for one batch.
    Only for roles where nobody is waiting on each answer; the per-step persona never uses it.
    """

    PRICE_FACTOR = 0.5

    def __init__(self, window: float = 3.0, poll: float = 15.0) -> None:
        super().__init__()
        self.window, self.poll = window, poll
        self._pending: list[tuple[LLMRequest, str, asyncio.Future]] = []
        self._flusher: asyncio.Task | None = None

    async def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending.append((request, model, future))
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush())
        return await future

    async def _flush(self) -> None:
        await asyncio.sleep(self.window)
        batch, self._pending, self._flusher = self._pending, [], None
        a = self._anthropic
        try:
            created = await self._client.messages.batches.create(requests=[
                {"custom_id": str(i), "params": self._params(req, model)} for i, (req, model, _) in enumerate(batch)])
            log.info("submitted batch %s with %d requests (half price; results usually within minutes)",
                     created.id, len(batch))
            while (await self._client.messages.batches.retrieve(created.id)).processing_status != "ended":
                await asyncio.sleep(self.poll)
            results = {}
            async for item in await self._client.messages.batches.results(created.id):
                results[item.custom_id] = item.result
        except (a.APIStatusError, a.APIConnectionError) as exc:
            for _, _, future in batch:
                if not future.done():
                    future.set_exception(LLMError(f"Anthropic batch failed: {exc}"))
            return
        for i, (_, model, future) in enumerate(batch):
            result = results.get(str(i))
            if future.done():
                continue
            if result is None or result.type != "succeeded":
                kind = getattr(result, "type", "missing")
                future.set_exception(LLMError(f"batch request {kind}"))
                continue
            try:
                response = self._parse(result.message, model)
                response.price_factor = self.PRICE_FACTOR
                future.set_result(response)
            except LLMError as exc:
                future.set_exception(exc)


def _media_type(img: bytes) -> str:
    if img[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if img[:4] == b"RIFF" and img[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class OllamaClient:
    """A local model served by Ollama (free). Uses its JSON-schema `format` option."""

    engine = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (local URL from config)
                return json.loads(resp.read().decode("utf-8"))
        except OSError as exc:
            raise LLMError(f"could not reach Ollama at {self.base_url}: {exc}") from exc

    async def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        message: dict[str, Any] = {"role": "user", "content": request.prompt}
        if request.images:
            message["images"] = [base64.standard_b64encode(i).decode("ascii") for i in request.images]
        payload = {
            "model": model, "stream": False, "format": request.schema,
            "options": {"seed": request.seed, "num_predict": request.max_tokens},
            "messages": [{"role": "system", "content": request.system}, message],
        }
        body = await asyncio.to_thread(self._post, payload)
        text = body.get("message", {}).get("content", "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ollama returned invalid JSON: {text[:200]}") from exc
        return LLMResponse(data=data, engine=self.engine, model=model,
                           input_tokens=int(body.get("prompt_eval_count", 0)),
                           output_tokens=int(body.get("eval_count", 0)))


class FakeLLM:
    """Scripted client for tests: ``responder(request) -> dict``."""

    engine = "fake"

    def __init__(self, responder: Callable[[LLMRequest], dict[str, Any]]):
        self.responder = responder
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, model: str) -> LLMResponse:
        self.calls.append(request)
        data = self.responder(request)
        tok_in = text_tokens(request.system + request.prompt) + sum(
            image_tokens(*request.image_size) for _ in request.images)
        return LLMResponse(data=data, engine=self.engine, model=model,
                           input_tokens=tok_in, output_tokens=text_tokens(json.dumps(data)))


# ------------------------------------------------------------------------ gateway


class LLMGateway:
    """Routes a request to the engine configured for its role, with cache and usage records."""

    def __init__(self, settings: Settings, db: Database | None = None, *,
                 clients: dict[str, LLMClient] | None = None, run_id: str | None = None):
        self.settings = settings
        self.db = db
        self.run_id = run_id
        self._clients: dict[str, LLMClient] = dict(clients or {})
        self.spent_usd = 0.0

    def engine_for(self, role: str) -> str:
        return self.settings.engine.engine_for(role)

    def is_model_backed(self, role: str) -> bool:
        return self.engine_for(role) != "heuristic"

    def _client(self, engine: str, role: str = "") -> LLMClient:
        if engine == "anthropic" and self.settings.engine.batch and role in self.settings.engine.batch_roles:
            if "anthropic-batch" not in self._clients:
                self._clients["anthropic-batch"] = AnthropicBatchClient()
            return self._clients["anthropic-batch"]
        if engine not in self._clients:
            if engine == "anthropic":
                self._clients[engine] = AnthropicClient()
            elif engine == "ollama":
                provider = self.settings.models.get("ollama")
                self._clients[engine] = OllamaClient((provider.base_url if provider else None)
                                                     or "http://127.0.0.1:11434")
            else:
                raise LLMError(f"no client available for engine {engine!r}")
        return self._clients[engine]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        engine, model = self.settings.model_for(request.role)
        if request.engine and request.engine != engine:
            engine = request.engine
            provider = self.settings.models.get(engine)
            model = (provider.roles.get(request.role) if provider else None) or engine
        if engine == "heuristic":
            raise LLMError(f"role {request.role!r} uses the heuristic engine; no model call is made")
        model = request.model or model
        if request.effort is None and (provider := self.settings.models.get(engine)) is not None:
            request.effort = provider.effort.get(request.role)
        key = request.cache_key(engine, model)
        if request.cacheable and (hit := self._cache_get(key)) is not None:
            self._record(request.role, engine, model, 0, 0, 0.0, cached=True)
            return LLMResponse(data=hit, engine=engine, model=model, cached=True)
        client = self._client(engine, request.role)
        response = await self._call_validated(client, request, model)
        response.cost_usd = response.price_factor * call_cost(
            self.settings, engine, model, response.input_tokens, response.output_tokens,
            cache_read=response.cache_read_tokens, cache_write=response.cache_write_tokens)
        self.spent_usd += response.cost_usd
        if request.cacheable:
            self._cache_put(key, model, response.data)
        self._record(request.role, engine, model, response.input_tokens, response.output_tokens,
                     response.cost_usd, cached=False)
        return response

    async def _call_validated(self, client: LLMClient, request: LLMRequest, model: str) -> LLMResponse:
        response = await client.complete(request, model)
        errors = validate_json(response.data, request.schema)
        if not errors:
            return response
        log.warning("%s output failed validation (%s); retrying once", request.role, "; ".join(errors[:3]))
        retry = LLMRequest(**{**request.__dict__, "prompt": request.prompt +
                              "\n\nYour previous answer was invalid: " + "; ".join(errors[:5]) +
                              "\nAnswer again, matching the schema exactly."})
        response = await client.complete(retry, model)
        errors = validate_json(response.data, request.schema)
        if errors:
            raise LLMError(f"{request.role} output invalid after retry: {'; '.join(errors[:5])}")
        return response

    # ------------------------------------------------------------------ storage
    def _cache_get(self, key: str) -> dict[str, Any] | None:
        if self.db is None:
            return None
        row = self.db.one("SELECT response_json FROM llm_cache WHERE key = ?", [key])
        return row["response_json"] if row else None

    def _cache_put(self, key: str, model: str, data: dict[str, Any]) -> None:
        if self.db is not None:
            self.db.insert("llm_cache", {"key": key, "model": model, "response_json": data,
                                         "created_at": utcnow()}, replace=True)

    def _record(self, role: str, engine: str, model: str, tin: int, tout: int, usd: float, *, cached: bool) -> None:
        if self.db is not None:
            self.db.insert("llm_usage", {"run_id": self.run_id, "role": role, "engine": engine,
                                         "model": model, "input_tokens": tin, "output_tokens": tout,
                                         "cost_usd": usd, "cached": cached, "created_at": utcnow()})
