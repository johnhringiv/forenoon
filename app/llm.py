"""The model calls: one interface, a backend per kind of connection, and a router between them.

A model is named with the connection it is reached through (`anthropic/claude-haiku-5-5`, see
app/models.py), and the router hands each call to that connection's backend. Anthropic is built
in. Every other connection is a server that speaks the OpenAI chat completions API (OpenAI
itself, LM Studio, Ollama, llama.cpp, vLLM), set up on the Settings page and kept in the
`connections` table, with its key in the environment.

The model receives article text as data and returns a structured answer. It has no tools that
act on anything, and its output is validated by the caller before use.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from . import models
from .config import Llm

API_KEY_ENV = "FORENOON_ANTHROPIC_API_KEY"
KEY_ENV_PREFIX = "FORENOON_LLM_KEY_"
MAX_OUTPUT_TOKENS = 8192  # a batch of 80 candidates needs about 3,300
MAX_LISTED = 500  # model ids kept from a server's list
CHECK_TIMEOUT = 2.0  # seconds for a connection's model list, at the start of a run
CHARS_PER_TOKEN = 4  # a rough count of a prompt's tokens, the same everywhere it is estimated
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT, as Tailscale uses: your own devices
CONTEXT_MARGIN = 512  # tokens kept spare in a request, for the estimate's error and the template


class LlmError(Exception):
    """A call that gave no usable answer. The tokens it used anyway are logged with it."""

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0, priced: bool = True):
        super().__init__(message)
        self.input_tokens, self.output_tokens, self.priced = input_tokens, output_tokens, priced


class Unreachable(LlmError):
    """The connection could not be used for this call: down, timed out, refused the key, or the
    server failed. `down` when it cannot be used at all (the host cannot be reached, the key is
    refused, or the start-of-run look at its model list failed): then it is not tried again in
    the run. A slow answer or a server error loses only the one call."""

    def __init__(self, message: str, *args, down: bool = False, timeout: bool = False, **kwargs):
        super().__init__(message, *args, **kwargs)
        self.down, self.timeout = down, timeout


class BadAnswer(LlmError):
    """The model answered, but not with something usable. Another model would not be asked."""


@dataclass(frozen=True)
class Reply:
    data: dict[str, Any]
    input_tokens: int  # read at the full price; the cached part is apart, below
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    priced: bool = True  # False for a server you run: costs nothing, outside the token cap


@dataclass(frozen=True)
class Connection:
    """An OpenAI-compatible server, as saved on the Settings page."""

    name: str
    address: str  # the API's base, as in http://192.168.1.20:8080/v1
    context_tokens: int = 32768
    output_tokens: int = 16384
    timeout: float = 300.0
    free: bool = True
    reasoning_off: bool = True

    @property
    def key_env(self) -> str:
        return key_env(self.name)


def key_env(connection: str) -> str:
    """The environment variable holding a connection's key: lan-box → FORENOON_LLM_KEY_LAN_BOX."""
    return KEY_ENV_PREFIX + connection.upper().replace("-", "_")


class Backend(Protocol):
    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        """One request to `model` whose answer must match `schema`. Raises LlmError.

        `shared` is text that comes before `user` and is the same across a run's calls; a
        backend may cache it.
        """


ASK_FOR_TOOL = "Give your answer by calling the `answer` tool exactly once, and write nothing else."


class AnthropicBackend:
    def __init__(self, cfg: Llm, api_key: str):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=cfg.timeout, max_retries=1)

    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        # A single call of a tool that does nothing is the answer format. It is forced where
        # the model allows that; a model that refuses it, or one not listed, is offered it and
        # asked for it in the system prompt.
        tool = {"name": "answer", "description": "Return the result.", "input_schema": schema}
        profile = models.profiles().get(model)
        choice: dict[str, str] = {"type": "tool", "name": "answer"}
        if profile is None or profile.tool_choice != "forced":
            choice, system = {"type": "auto"}, f"{system}\n\n{ASK_FOR_TOOL}"
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if shared:
            # Marked for the prompt cache, so later batches of a run pay a tenth for it. The
            # API ignores the mark when the text is under the model's minimum cacheable size.
            content.insert(0, {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}})
        try:
            response = self._client.messages.create(
                model=models.split(model)[1],
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": content}],
                tools=[tool],
                tool_choice=choice,
            )
        except self._anthropic.AuthenticationError as exc:
            raise Unreachable(f"{type(exc).__name__}: {exc}", down=True) from exc
        except self._anthropic.APIConnectionError as exc:
            # Timeouts and dropped connections included: the API is up, this call is lost.
            slow = isinstance(exc, self._anthropic.APITimeoutError)
            raise Unreachable(f"{type(exc).__name__}: {exc}", timeout=slow) from exc
        except self._anthropic.APIStatusError as exc:
            # Overloaded or rate limited: this call is lost, the next may go through.
            busy = exc.status_code in (429, 529)
            kind = LlmError if busy else Unreachable if exc.status_code >= 500 or exc.status_code == 403 else BadAnswer
            raise kind(f"{type(exc).__name__}: {exc}") from exc
        except self._anthropic.APIError as exc:
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc
        usage = response.usage
        # Cached input is reported, and priced, apart from the rest; the daily cap counts all of it.
        written, read = usage.cache_creation_input_tokens or 0, usage.cache_read_input_tokens or 0
        for block in response.content:
            if block.type == "tool_use" and isinstance(block.input, dict):
                return Reply(block.input, usage.input_tokens, usage.output_tokens, written, read)
        raise BadAnswer(f"no structured answer (stop reason: {response.stop_reason})")


# The OpenAI chat completions API, as OpenAI and the local servers speak it.


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` as OpenAI's strict mode accepts it: every object closed, every property required,
    and the ones that were optional allowed to be null instead. llama.cpp and vLLM enforce the
    same subset by grammar."""
    out = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            required = set(node.get("required", []))
            for name, prop in node["properties"].items():
                visit(prop)
                if name not in required and isinstance(prop, dict) and "type" in prop:
                    kinds = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
                    prop["type"] = [*kinds, "null"] if "null" not in kinds else kinds
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        if isinstance(node.get("items"), dict):
            visit(node["items"])

    visit(out)
    return out


def _without_nulls(data: Any, schema: dict[str, Any]) -> Any:
    """Drop the nulls strict mode put in for optional properties, so callers see them absent."""
    if isinstance(data, dict) and isinstance(schema.get("properties"), dict):
        required = set(schema.get("required", []))
        return {
            key: _without_nulls(value, schema["properties"].get(key, {}))
            for key, value in data.items()
            if not (value is None and key not in required)
        }
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        return [_without_nulls(item, schema["items"]) for item in data]
    return data


def _answer_text(content: str) -> str:
    """The JSON in a reply: reasoning a server left in the text (also when the opening <think>
    was in the prompt, so only the closing one is here), and anything around the object, such
    as a Markdown fence or a sentence before it, removed."""
    text = content.rpartition("</think>")[2].strip() if "</think>" in content else content.strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end and not text.startswith("{") else text


class OpenAIBackend:
    """One OpenAI-compatible connection. Requests go one at a time, as a local server prefers."""

    def __init__(self, connection: Connection, api_key: str = "", client: httpx.Client | None = None):
        self.connection = connection
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=connection.timeout)
        self._headers = headers
        self._base = connection.address.rstrip("/")
        self._openai = urlsplit(self._base).hostname == "api.openai.com"

    @property
    def priced(self) -> bool:
        """Whether its calls cost money: not for a server you run, which is one on a private,
        loopback or local address marked free. A service on the internet always counts as paid,
        whatever the box says, so the daily token cap still guards it."""
        return not (self.connection.free and private_host(self._base))

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                f"{self._base}/chat/completions", json=body, headers=self._headers, timeout=self.connection.timeout
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Refused or not there at all is down; a server too slow for this request is not.
            unreached = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
            down = isinstance(exc, (*unreached, httpx.InvalidURL))
            slow = isinstance(exc, httpx.TimeoutException) and not down
            raise Unreachable(f"{self.connection.name}: {type(exc).__name__}: {exc}", down=down, timeout=slow) from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            # A server that is up but turns the request down is a bad answer, except for the
            # refusals that another connection would not get: the key, the server's own trouble.
            # A rate limit is neither: this call is lost, the next may go through.
            unusable = response.status_code in (403, 404) or response.status_code >= 500
            message = f"{self.connection.name}: HTTP {response.status_code}: {detail}"
            if response.status_code == 401:
                raise Unreachable(message, down=True)  # the key: no call will get through
            # 403 is often about this request (moderation) or this model, not the connection.
            kind = LlmError if response.status_code == 429 else Unreachable if unusable else BadAnswer
            raise kind(message)
        try:
            reply = response.json()
        except ValueError as exc:
            raise BadAnswer(f"{self.connection.name}: the reply is not JSON") from exc
        if not isinstance(reply, dict):
            raise BadAnswer(f"{self.connection.name}: the reply is not a JSON object")
        return reply

    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        name = models.split(model)[1]
        strict = strict_schema(schema)
        # Servers that enforce the schema by grammar never show it to the model, so it is in
        # the prompt as well. The shared part comes first: a server that caches a repeated
        # opening reuses it across the run's batches.
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": f"{system}\n\nAnswer with JSON only, matching this schema:\n{json.dumps(strict)}",
            },
            {"role": "user", "content": shared + user},
        ]
        body: dict[str, Any] = {
            "model": name,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": strict},
            },
        }
        # OpenAI's reasoning models take only max_completion_tokens and the default temperature.
        if self._openai:
            body["max_completion_tokens"] = self.connection.output_tokens
        else:
            body["temperature"] = 0
            if self.connection.reasoning_off:
                # Read by llama.cpp, vLLM and LM Studio's chat templates (Qwen, DeepSeek and the like).
                body["chat_template_kwargs"] = {"enable_thinking": False}
        input_tokens = output_tokens = cached = 0
        problem = ""

        def spent() -> tuple[int, int, bool]:
            return input_tokens + cached, output_tokens, self.priced

        for attempt in range(2):
            if not self._openai:
                # A server may refuse a request whose prompt and answer limit together pass its
                # context. Batches leave room for the whole answer limit (input_budget), so this
                # only shortens it for a prompt longer than its estimate.
                prompt = sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN
                room = self.connection.context_tokens - prompt - CONTEXT_MARGIN
                body["max_tokens"] = max(256, min(self.connection.output_tokens, room))
            reply = self._post(body)
            usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
            details = usage.get("prompt_tokens_details")
            hit = (details.get("cached_tokens") if isinstance(details, dict) else 0) or 0
            input_tokens += (usage.get("prompt_tokens") or 0) - hit
            cached += hit
            output_tokens += usage.get("completion_tokens") or 0
            try:
                choice = reply["choices"][0]
                content = choice["message"].get("content") or ""
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                raise BadAnswer(f"{self.connection.name}: no answer in the reply", *spent()) from exc
            if choice.get("finish_reason") == "length":
                # Asking again would stop in the same place.
                limit = body.get("max_tokens", body.get("max_completion_tokens"))
                short = limit != self.connection.output_tokens
                fix = "raise the context size" if short else "raise the answer limit"
                raise BadAnswer(
                    f"{self.connection.name}: the answer was cut off at {limit} tokens; {fix} on the Settings page",
                    *spent(),
                )
            try:
                data = json.loads(_answer_text(content))
            except ValueError as exc:
                data, problem = None, f"the answer is not valid JSON ({exc})"
            else:
                problem = "" if isinstance(data, dict) else "the answer is not a JSON object"
            if problem:
                if attempt == 0:
                    # Once more, saying what was wrong.
                    messages += [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": f"That is not valid: {problem}. Answer again with the JSON only."},
                    ]
                continue
            return Reply(_without_nulls(data, schema), input_tokens, output_tokens, 0, cached, self.priced)
        raise BadAnswer(f"{self.connection.name}: {problem}", *spent())

    def model_ids(self, timeout: float = CHECK_TIMEOUT, limit: int | None = MAX_LISTED) -> list[str]:
        """The models the server lists, by the ids it knows them by. Raises Unreachable."""
        try:
            response = self._client.get(f"{self._base}/models", headers=self._headers, timeout=timeout)
            response.raise_for_status()
            listed = response.json().get("data") or []
            if not isinstance(listed, list):
                raise TypeError("no list of models")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, AttributeError) as exc:
            raise Unreachable(f"{self.connection.name}: {type(exc).__name__}: {exc}") from exc
        ids = [m["id"] for m in listed if isinstance(m, dict) and isinstance(m.get("id"), str)]
        # A model setting cannot name one with a space in it; and the page lists them all.
        return [i for i in ids if i and not any(c.isspace() for c in i) and len(i) <= 150][:limit]


class Router:
    """Sends each call to the backend of the connection its model names."""

    def __init__(self, backends: dict[str, Backend]):
        self._backends = backends
        # Connections found unreachable in this run, with why: not tried again until the next,
        # so a server that is off costs one time limit, not one per call. A connection whose
        # calls time out twice in a row joins them: too slow for this run's requests.
        self._down: dict[str, str] = {}
        self._timeouts: dict[str, int] = {}

    def backend(self, model: str) -> Backend:
        try:
            connection = models.split(model)[0]
        except ValueError as exc:
            raise LlmError(str(exc)) from exc
        backend = self._backends.get(connection)
        if backend is None:
            raise Unreachable(f"no connection named {connection!r} for the model {model}", down=True)
        return backend

    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        backend = self.backend(model)
        connection = models.split(model)[0]
        if connection in self._down:
            raise Unreachable(f"{self._down[connection]} (not tried again this run)", down=True)
        try:
            reply = backend.structured(system, user, schema, model=model, shared=shared)
        except Unreachable as exc:
            self._timeouts[connection] = self._timeouts.get(connection, 0) + 1 if exc.timeout else 0
            if exc.down or self._timeouts[connection] >= 2:
                self._down[connection] = str(exc) if exc.down else f"{exc}; timed out twice in a row"
                exc.down = True
            raise
        except LlmError:
            self._timeouts[connection] = 0
            raise
        self._timeouts[connection] = 0
        return reply

    def check(self, model: str) -> str:
        """Why `model` cannot be used now, or "" when it can: a quick look at its connection's
        model list. Anthropic is taken as reachable when its key is set; it is only found out
        by its calls."""
        try:
            backend = self.backend(model)
        except LlmError as exc:
            return str(exc)
        connection = models.split(model)[0]
        if connection in self._down:
            return self._down[connection]
        if not isinstance(backend, OpenAIBackend):
            return ""
        try:
            listed = backend.model_ids(limit=None)
        except Unreachable as exc:
            self._down[connection] = str(exc)
            return str(exc)
        name = models.split(model)[1]
        return "" if not listed or name in listed else f"{backend.connection.name} does not list the model {name}"


def free(backend: Backend | None, model: str) -> bool:
    """Calls to `model` cost nothing (its connection is a server you run), so the daily token
    cap, which guards spending, does not hold them back."""
    if not isinstance(backend, Router):
        return False
    try:
        connection = backend.backend(model)
    except LlmError:
        return False
    return isinstance(connection, OpenAIBackend) and not connection.priced


def input_budget(backend: Backend | None, model: str) -> int | None:
    """How many prompt tokens one request to `model` may hold, when its connection has a
    context size (a server on the network): the context less room for the answer. None when
    there is no such limit to keep to. A local server may drop what does not fit without saying,
    so batches are sized to this."""
    if not isinstance(backend, Router):
        return None
    try:
        connection = backend.backend(model)
    except LlmError:
        return None
    if not isinstance(connection, OpenAIBackend):
        return None
    # The whole answer limit is kept free, the way the request asks for it (see structured).
    return max(1024, connection.connection.context_tokens - connection.connection.output_tokens - CONTEXT_MARGIN)


def private_host(address: str) -> bool:
    """Whether an address is on this network: a private, loopback or link-local IP or one on a
    Tailscale network, a name without a dot (gpu-box, localhost), or one under .local, .lan,
    .home.arpa, .internal or .ts.net."""
    host = (urlsplit(address).hostname or "").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return bool(host) and (
            "." not in host or host.endswith((".local", ".lan", ".home.arpa", ".internal", ".ts.net"))
        )
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in TAILNET


def models_for_run(cfg: Llm, backend: Backend | None) -> tuple[Llm, str]:
    """The models this run uses, decided once at its start: each one whose connection cannot be
    reached is replaced by `cfg.fallback_model`, when that one can be. With what was found, for
    the report and the alert ("" when every model was there). Only an unreachable connection
    moves a run on: a bad answer from a reachable model is that model's to own."""
    if not isinstance(backend, Router):
        return cfg, ""
    found: dict[str, str] = {}

    def problem(model: str) -> str:
        if model not in found:
            found[model] = backend.check(model)
        return found[model]

    changes: dict[str, str] = {}
    notes: list[str] = []
    for setting in ("model", "choose_model"):
        model = getattr(cfg, setting)
        if not model or not (why := problem(model)):
            continue
        fallback = cfg.fallback_model
        if fallback and fallback != model and not problem(fallback):
            changes[setting] = fallback
            notes.append(f"{model} could not be reached ({why}), so {fallback} was used")
        elif fallback and fallback != model:
            notes.append(f"{model} could not be reached ({why}), nor the fallback {fallback} ({problem(fallback)})")
        else:
            notes.append(f"{model} could not be reached ({why}) and there is no fallback model")
    return replace(cfg, **changes), "; ".join(dict.fromkeys(notes))


def backend_from_env(cfg: Llm, connections: tuple[Connection, ...] = ()) -> Backend | None:
    """The connections there are keys for, or None when there are none: then the run falls back
    to newest first and says so in its alert. A server on the network may need no key."""
    backends: dict[str, Backend] = {}
    if key := os.environ.get(API_KEY_ENV, ""):
        backends["anthropic"] = AnthropicBackend(cfg, key)
    for connection in connections:
        backends[connection.name] = OpenAIBackend(connection, os.environ.get(connection.key_env, ""))
    return Router(backends) if backends else None
