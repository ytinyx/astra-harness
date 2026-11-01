"""Opt-in OpenAI-compatible embeddings; configuration is not a connection.

Endpoints are host-chosen configuration, never model arguments. HTTPS names
must be trusted by the operator: URL checks are not DNS pinning or an OS network
sandbox. HTTP is restricted to explicit numeric loopback addresses. Each call
owns its HTTPX client because callers may use different event loops.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx


MAX_BATCH = 8
MAX_TEXT_CHARS = 1000
MAX_DIMENSIONS = 4096
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_USAGE_TOKENS = 1_000_000_000_000
DEFAULT_KEY_ENV = "ASTRA_EMBEDDING_API_KEY"
_KEY_ENV = re.compile(r"ASTRA_EMBEDDING_(?:[A-Z][A-Z0-9_]{0,48}_)?API_KEY\Z")


class EmbeddingError(RuntimeError):
    """Fixed diagnostics only; issued requests may have incurred a charge."""
    def __init__(self, code, request_issued=False):
        self.code = code
        self.request_issued = bool(request_issued)
        super().__init__(code)


def validate_embedding_endpoint(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in "\\?#%")):
        raise ValueError("invalid_embedding_endpoint")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)
                or not re.fullmatch(r"/[A-Za-z0-9._~/-]*embeddings", parsed.path)
                or "//" in parsed.path or any(p in {".", ".."} for p in parsed.path.split("/"))):
            raise ValueError
        if parsed.scheme == "http":
            if host not in {"127.0.0.1", "::1"} or port is None:
                raise ValueError
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if (len(host) > 253 or host.endswith(".") or "." not in host
                        or re.fullmatch(r"[0-9.]+", host)
                        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
                        or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in host.split("."))):
                    raise ValueError
            else:
                if not address.is_global:
                    raise ValueError
        authority = "[" + host + "]" if ":" in host else host
        if port is not None and not (parsed.scheme == "https" and port == 443):
            authority += ":" + str(port)
        return parsed.scheme + "://" + authority + parsed.path
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_embedding_endpoint") from None


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = False
    endpoint: str = ""
    model: str = ""
    key_env: str = DEFAULT_KEY_ENV
    dimensions: int | None = None
    revision: str = "1"
    timeout: float = 10.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("invalid_embedding_config")
        if not isinstance(self.endpoint, str):
            raise ValueError("invalid_embedding_config")
        if self.endpoint:
            object.__setattr__(self, "endpoint", validate_embedding_endpoint(self.endpoint))
        if (not isinstance(self.model, str) or len(self.model) > 128
                or any(ord(c) < 33 or ord(c) > 126 for c in self.model)
                or not isinstance(self.key_env, str) or _KEY_ENV.fullmatch(self.key_env) is None
                or (self.dimensions is not None and (type(self.dimensions) is not int
                                                     or not 1 <= self.dimensions <= MAX_DIMENSIONS))
                or not isinstance(self.revision, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.revision) is None
                or type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 60 or (self.enabled and (not self.endpoint or not self.model))):
            raise ValueError("invalid_embedding_config")
        object.__setattr__(self, "timeout", float(self.timeout))

    def public_config(self):
        return {"enabled": self.enabled, "endpoint": self.endpoint, "model": self.model,
                "key_env": self.key_env, "dimensions": self.dimensions,
                "revision": self.revision, "timeout": self.timeout}

    def identity(self):
        value = {"adapter": "openai-compatible-embeddings-v1", **self.public_config(),
                 "max_batch": MAX_BATCH, "max_text_chars": MAX_TEXT_CHARS,
                 "max_dimensions": MAX_DIMENSIONS, "max_response_bytes": MAX_RESPONSE_BYTES}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_embedding_config(value):
    if value is None:
        return EmbeddingConfig()
    if type(value) is not dict or set(value) - {"enabled", "endpoint", "model", "key_env", "dimensions", "revision", "timeout"}:
        raise ValueError("invalid_embedding_config")
    try:
        return EmbeddingConfig(**value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid_embedding_config") from None


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    usage: dict | None = None


class EmbeddingProvider(Protocol):
    """Replace this boundary without changing the Agent loop or vector index."""
    config: EmbeddingConfig

    def status(self) -> dict: ...

    def recovery_identity(self) -> str: ...

    async def embed(self, texts: list[str] | tuple[str, ...]) -> EmbeddingBatch: ...


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite")


def _result(raw, config, count):
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
        if (type(data) is not dict or "error" in data or data.get("object", "list") != "list"
                or type(data.get("data")) is not list or len(data["data"]) != count):
            raise ValueError
        if data.get("model") != config.model:
            raise EmbeddingError("embedding_model_mismatch")
        vectors, dimension = {}, config.dimensions
        for item in data["data"]:
            if (type(item) is not dict or item.get("object", "embedding") != "embedding"
                    or type(item.get("index")) is not int or not 0 <= item["index"] < count
                    or item["index"] in vectors or type(item.get("embedding")) is not list):
                raise ValueError
            vector = item["embedding"]
            if not 1 <= len(vector) <= MAX_DIMENSIONS:
                raise EmbeddingError("embedding_dimension_mismatch")
            if dimension is None:
                dimension = len(vector)
            if len(vector) != dimension:
                raise EmbeddingError("embedding_dimension_mismatch")
            if any(type(number) not in (int, float) or not math.isfinite(number) for number in vector):
                raise ValueError
            values = tuple(float(number) for number in vector)
            norm = math.hypot(*values)
            if not norm or not math.isfinite(norm):
                raise ValueError
            vectors[item["index"]] = values
        if set(vectors) != set(range(count)):
            raise ValueError
        usage = data.get("usage")
        if usage is not None:
            if type(usage) is not dict:
                raise ValueError
            usage = {key: usage[key] for key in ("prompt_tokens", "total_tokens") if key in usage}
            if any(type(n) is not int or not 0 <= n <= MAX_USAGE_TOKENS for n in usage.values()):
                raise ValueError
            if "prompt_tokens" in usage and "total_tokens" in usage and usage["total_tokens"] < usage["prompt_tokens"]:
                raise ValueError
        return EmbeddingBatch(tuple(vectors[i] for i in range(count)), data["model"], usage)
    except EmbeddingError:
        raise
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise EmbeddingError("embedding_protocol_error") from None


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, config: EmbeddingConfig, *, environ=None):
        if not isinstance(config, EmbeddingConfig):
            raise ValueError("invalid_embedding_config")
        self.config = config
        source = os.environ if environ is None else environ
        # Fix the credential for this host instance. It never enters public
        # configuration, provider identity, task arguments or returned metadata.
        self._api_key = source.get(config.key_env, "") if config.enabled else ""

    def status(self):
        present = isinstance(self._api_key, str) and bool(self._api_key)
        if not self.config.enabled:
            state = "not_configured"
        elif not present:
            state = "missing_key"
        elif len(self._api_key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self._api_key):
            state = "invalid_key"
        else:
            state = "ready"
        return {"state": state, "key_present": present}

    def recovery_identity(self):
        # This opaque, profile-scoped authorization identity changes after a
        # credential rotation. It is not a standalone credential hash, and is
        # deliberately separate from the index-content configuration identity.
        key = self._api_key.encode("utf-8", errors="surrogatepass") if isinstance(self._api_key, str) else b""
        domain = b"astra-embedding-authorization-v1\0"
        marker = self.status()["state"].encode("ascii")
        return hmac.new(key, domain + marker + b"\0" + self.config.identity().encode("ascii"), hashlib.sha256).hexdigest()

    def _client(self):
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        return httpx.AsyncClient(transport=transport, timeout=self.config.timeout,
                                 follow_redirects=False, trust_env=False)

    async def embed(self, texts):
        state = self.status()["state"]
        if state != "ready":
            raise EmbeddingError("embedding_" + state)
        if (not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= MAX_BATCH
                or any(not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS
                       or "\0" in text for text in texts)):
            raise EmbeddingError("embedding_invalid_input")
        try:
            payload = {"model": self.config.model, "input": list(texts), "encoding_format": "float"}
            if self.config.dimensions is not None:
                payload["dimensions"] = self.config.dimensions
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise EmbeddingError("embedding_invalid_input") from None
        issued = False

        async def request():
            nonlocal issued
            async with self._client() as client:
                req = client.build_request("POST", self.config.endpoint, content=body,
                    headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
                             "Accept": "application/json", "Accept-Encoding": "identity"})
                req.headers.pop("cookie", None)
                issued = True
                response = await client.send(req, stream=True, follow_redirects=False)
                try:
                    if response.status_code in (401, 403):
                        raise EmbeddingError("embedding_authentication_failed")
                    if response.status_code == 429:
                        raise EmbeddingError("embedding_rate_limited")
                    if response.status_code != 200:
                        raise EmbeddingError("embedding_provider_error")
                    if (sum(len(k)+len(v) for k, v in response.headers.raw) > 16384
                            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
                            or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
                        raise EmbeddingError("embedding_protocol_error")
                    lengths = response.headers.get_list("content-length")
                    if lengths:
                        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
                            raise EmbeddingError("embedding_protocol_error")
                        if int(lengths[0]) > MAX_RESPONSE_BYTES:
                            raise EmbeddingError("embedding_response_limit")
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw)+len(chunk) > MAX_RESPONSE_BYTES:
                            raise EmbeddingError("embedding_response_limit")
                        raw.extend(chunk)
                    return _result(raw, self.config, len(texts))
                finally:
                    await response.aclose()

        try:
            return await asyncio.wait_for(request(), self.config.timeout)
        except asyncio.CancelledError:
            raise
        except EmbeddingError as error:
            raise EmbeddingError(error.code, request_issued=issued) from None
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise EmbeddingError("embedding_timeout", request_issued=issued) from None
        except Exception:
            raise EmbeddingError("embedding_transport_error", request_issued=issued) from None
