"""Strict, explicitly selected Chat Completions profiles over the existing adapter.

Configuration parsing performs no DNS or HTTP. An explicit request checks all
DNS answers before sending credentials. HTTPX then resolves independently: this
has a DNS-rebinding/TOCTOU gap and is not complete SSRF protection. Only configure
services you trust; this module does not pin DNS, disable TLS, or use env proxies.
"""

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import re
import socket
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx

from ..contracts import Message, ModelRequest
from .async_models import AsyncOpenAICompatibleModel
from .models import ModelAdapterError

MAX_RESPONSE_BYTES = 2_000_000
MAX_REQUEST_BYTES = 1_000_000
MAX_HEADER_BYTES = 16384
_KEY_ENV = re.compile(r"(?:ASTRA_API_KEY|ASTRA_MODEL_(?:[A-Z][A-Z0-9_]{0,48}_)?API_KEY)\Z")
_TEST_ERRORS = frozenset({"model_credentials_missing", "model_credentials_invalid", "model_endpoint_not_public",
    "model_dns_failed", "model_request_too_large", "model_authentication_failed", "model_rate_limited",
    "model_transport_error", "model_response_too_large", "model_protocol_error", "model_timeout",
    "model_test_unexpected_tools"})


def _public_address(host):
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_unspecified and not address.is_loopback and not address.is_link_local
            and not getattr(address, "is_site_local", False))


def validate_profile_base_url(value):
    """Pure URL validation. This returns a base URL, not /chat/completions."""
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in "\\?#%")):
        raise ValueError("invalid_model_base_url")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        path = parsed.path.rstrip("/")
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.netloc.endswith(":")
                or (port is not None and not 1 <= port <= 65535)
                or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", path)
                or any(part in {".", ".."} for part in path.split("/"))
                or path.endswith("/chat/completions") or "//" in parsed.path):
            raise ValueError
        if parsed.scheme == "http":
            if host not in {"127.0.0.1", "::1"} or port is None:
                raise ValueError
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if (len(host) > 253 or "." not in host or host.endswith(".")
                        or re.fullmatch(r"[0-9.]+", host)
                        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
                        or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                   for part in host.split("."))):
                    raise ValueError
            else:
                if not _public_address(host):
                    raise ValueError
                host = address.compressed
        authority = "[" + host + "]" if ":" in host else host
        if port is not None and not (parsed.scheme == "https" and port == 443):
            authority += ":" + str(port)
        return parsed.scheme + "://" + authority + path
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_model_base_url") from None


def validate_profile_config(value):
    """Normalize only non-secret adapter fields; never look up an env value."""
    fields = {"provider", "base_url", "model", "key_env", "timeout", "max_output_tokens"}
    if type(value) is not dict or set(value) - fields or not {"base_url", "model"} <= value.keys():
        raise ValueError("invalid_model_profile")
    result = {"provider": "chat_completions", "key_env": "ASTRA_MODEL_API_KEY",
              "timeout": 30.0, "max_output_tokens": 2048, **value}
    if (result["provider"] != "chat_completions" or not isinstance(result["model"], str)
            or not 1 <= len(result["model"]) <= 128
            or any(ord(c) < 33 or ord(c) > 126 for c in result["model"])
            or (result["key_env"] is not None and (not isinstance(result["key_env"], str)
                or _KEY_ENV.fullmatch(result["key_env"]) is None))
            or type(result["timeout"]) not in (int, float) or not 1 <= result["timeout"] <= 120
            or type(result["max_output_tokens"]) is not int or not 1 <= result["max_output_tokens"] <= 32768):
        raise ValueError("invalid_model_profile")
    result["base_url"] = validate_profile_base_url(result["base_url"])
    result["timeout"] = float(result["timeout"])
    return result


class AsyncProfileModel(AsyncOpenAICompatibleModel):
    """Protocol adapter only. The host owns profile identity and credentials."""
    def __init__(self, api_key, config, *, max_response_bytes=MAX_RESPONSE_BYTES):
        config = validate_profile_config(config)
        if not isinstance(api_key, str) or not api_key:
            raise ModelAdapterError("model_credentials_missing")
        if len(api_key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise ModelAdapterError("model_credentials_invalid")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("invalid_model_configuration")
        super().__init__(api_key, config["model"], config["base_url"], config["timeout"],
                         max_response_bytes, max_output_tokens=config["max_output_tokens"])
        self.profile_config = MappingProxyType(config)
        self.request_issued = False

    async def _check_destination(self):
        parsed = urlsplit(self.base_url)
        if parsed.scheme == "http":
            return  # Pure validation already allowed only explicit numeric loopback.
        try:
            ipaddress.ip_address(parsed.hostname)
            return
        except ValueError:
            pass
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            if (not answers or len(answers) > 32
                    or any(answer[0] not in {socket.AF_INET, socket.AF_INET6}
                           or not _public_address(answer[4][0]) for answer in answers)):
                raise ModelAdapterError("model_endpoint_not_public")
        except ModelAdapterError:
            raise
        except (OSError, ValueError, TypeError, IndexError):
            raise ModelAdapterError("model_dns_failed") from None

    @asynccontextmanager
    async def _response(self, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ModelAdapterError("model_request_too_large")
        await self._check_destination()
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json",
                   "Accept-Encoding": "identity", "Accept": "text/event-stream" if payload["stream"] else "application/json"}
        try:
            async with self._client() as client:
                # An issued request may be charged even if cancellation, timeout
                # or a dropped response prevents observing its result.
                self.request_issued = True
                async with client.stream("POST", self.url, content=body, headers=headers) as response:
                    if response.status_code in (401, 403):
                        raise ModelAdapterError("model_authentication_failed")
                    if response.status_code == 429:
                        raise ModelAdapterError("model_rate_limited")
                    if not 200 <= response.status_code < 300:
                        raise ModelAdapterError("model_transport_error")
                    if sum(len(k) + len(v) for k, v in response.headers.raw) > MAX_HEADER_BYTES:
                        raise ModelAdapterError("model_response_too_large")
                    expected_type = "text/event-stream" if payload["stream"] else "application/json"
                    if (response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected_type
                            or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
                        raise ModelAdapterError("model_protocol_error")
                    lengths = response.headers.get_list("content-length")
                    if lengths:
                        if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,12}", lengths[0]) is None:
                            raise ModelAdapterError("model_protocol_error")
                        if int(lengths[0]) > self.max_response_bytes:
                            raise ModelAdapterError("model_response_too_large")
                    yield response
        except httpx.TimeoutException:
            raise ModelAdapterError("model_timeout") from None

    async def _bounded(self, operation):
        self.request_issued = False
        try:
            return await asyncio.wait_for(operation, self.timeout)
        except asyncio.TimeoutError:
            raise ModelAdapterError("model_timeout") from None

    async def complete(self, request):
        return await self._bounded(super().complete(request))

    async def stream(self, request, on_delta):
        return await self._bounded(super().stream(request, on_delta))


def build_profile_model(api_key, config):
    return AsyncProfileModel(api_key, config)


async def probe_model(model):
    """Test a frozen host binding without reading or rotating its credential.

    A separate request-scoped adapter applies the smaller probe limits; neither
    its response nor those limits change an already running task's model.
    """
    if not isinstance(model, AsyncProfileModel):
        raise ValueError("invalid_model_profile")
    return await test_profile_model(model.api_key, dict(model.profile_config))


async def test_profile_model(api_key, config):
    """One explicit synthetic request, not a capability/quality/cost guarantee."""
    config = validate_profile_config(config)
    test_config = {**config, "timeout": min(config["timeout"], 10.0),
                   "max_output_tokens": min(config["max_output_tokens"], 32)}
    model = None
    try:
        model = AsyncProfileModel(api_key, test_config, max_response_bytes=65536)
        response = await model.complete(ModelRequest((Message("user", "Reply with OK."),), ()))
        if response.tool_calls:
            raise ModelAdapterError("model_test_unexpected_tools")
        return {"ok": True, "request_issued": model.request_issued, "requested_model": config["model"],
                "usage": response.usage}
    except asyncio.CancelledError:
        raise
    except ModelAdapterError as error:
        code = str(error) if str(error) in _TEST_ERRORS else "model_transport_error"
        return {"ok": False, "error_code": code, "request_issued": model.request_issued if model else False,
                "requested_model": config["model"]}
