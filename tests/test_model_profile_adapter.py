"""Profile protocol fixtures only; no real environment credentials or external I/O."""

import asyncio
from contextlib import asynccontextmanager
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from astra_harness.builtin.async_models import AsyncOpenAICompatibleModel
from astra_harness.builtin.models import ModelAdapterError, OpenAICompatibleModel
from astra_harness.builtin.model_profile_adapter import (
    AsyncProfileModel, build_profile_model, test_profile_model as probe_profile,
    probe_model, validate_profile_base_url, validate_profile_config)
from astra_harness.contracts import Message, ModelRequest
from tests.test_async_models import Pieces, RecordingTransport, completion, frame, chunk, USAGE

REQUEST = ModelRequest((Message("user", "Synthetic protocol check"),), ())
KEY = "synthetic-profile-key-not-real"


def config(base_url="http://127.0.0.1:12345", **changes):
    return {"base_url": base_url, "model": "fixture-model", **changes}


def attach(model, response_factory):
    clients, transports = [], []
    def client():
        transport = RecordingTransport(response_factory)
        value = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
        clients.append(value)
        transports.append(transport)
        return value
    model._client = client
    return clients, transports


def json_response(value=None, *, status=200, headers=None, stream=None):
    return httpx.Response(status, headers=headers or {"content-type": "application/json"},
                          stream=stream or Pieces([json.dumps(completion() if value is None else value).encode()]))


@asynccontextmanager
async def local_http(respond):
    received, tasks = [], set()
    async def handler(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            lines = head.decode("ascii").split("\r\n")
            headers = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
            raw = await reader.readexactly(int(headers["content-length"]))
            received.append({"line": lines[0], "headers": headers, "body": json.loads(raw)})
            await respond(reader, writer, received[-1])
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(asyncio.current_task())
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", received
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


class ProfileConfigurationTests(unittest.TestCase):
    def test_pure_defaults_and_normalization_do_not_touch_environment_or_network(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS during save")), \
             patch("httpx.AsyncClient", side_effect=AssertionError("HTTP during save")), \
             patch("os.environ", {}):
            result = validate_profile_config(config("HTTPS://MODELS.example.COM:443/v1/"))
            model = build_profile_model(KEY, result)
        self.assertEqual(result, {"provider": "chat_completions", "base_url": "https://models.example.com/v1",
            "model": "fixture-model", "key_env": "ASTRA_MODEL_API_KEY", "timeout": 30.0, "max_output_tokens": 2048})
        self.assertFalse(model.request_issued)
        result["model"] = "changed-after-construction"
        self.assertEqual(model.model, "fixture-model")
        with self.assertRaises(TypeError):
            model.profile_config["model"] = "mutate"

    def test_endpoint_matrix_is_static(self):
        accepted = {"https://api.deepseek.com/": "https://api.deepseek.com",
                    "https://api.openai.com/v1": "https://api.openai.com/v1",
                    "http://127.0.0.1:8080/v1/": "http://127.0.0.1:8080/v1",
                    "http://[::1]:8080": "http://[::1]:8080", "https://1.1.1.1": "https://1.1.1.1"}
        for value, expected in accepted.items():
            self.assertEqual(validate_profile_base_url(value), expected)
        denied = ["http://localhost:8000", "http://127.0.0.1", "http://127.1:80", "http://example.com:80",
            "https://user:secret@example.com/v1", "https://@example.com", "https://example.com?q=x", "https://example.com#x",
            "https://example.com/v1%2fanything", "https://example.com/../v1", "https://example.com/v1//", "https://example.com\\x",
            "https://example.com/v1/chat/completions", "https://example.com:", "https://example.com:0", "https://example.com:65536",
            "https://localhost", "https://service.local", "https://metadata.google.internal", "https://169.254.169.254",
            "https://10.0.0.1", "https://192.168.1.1", "https://127.0.0.1", "https://[::1]", "https://[fe80::1]",
            "https://[::ffff:127.0.0.1]", "https://224.0.0.1", "https://100.64.0.1", "https://2130706433",
            "https://example.com\n", "https://example.com/ x", "https://example.com./v1"]
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS during validation")):
            for value in denied:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^invalid_model_base_url$"):
                    validate_profile_base_url(value)

    def test_fields_and_credentials_reject_invalid_values(self):
        changes = [{"timeout": value} for value in (True, 0, 121, float("inf"), float("nan"), 10**1000)]
        changes += [{"max_output_tokens": value} for value in (True, 0, 32769, 1.5)]
        changes += [{"provider": "anthropic"}, {"model": "has space"}, {"api_key": "must-not-save"},
                    {"key_env": "ASTRA_EMBEDDING_API_KEY"}, {"key_env": "PATH"}, {"key_env": "OPENAI_API_KEY"}]
        for change in changes:
            with self.subTest(keys=list(change)), self.assertRaises(ValueError):
                validate_profile_config(config(**change))
        for key in ("ASTRA_API_KEY", "ASTRA_MODEL_API_KEY", "ASTRA_MODEL_WORK_API_KEY"):
            self.assertEqual(validate_profile_config(config(key_env=key))["key_env"], key)
        for key in ("", None, "secret\r\ninjection", "a" * 4097):
            with self.assertRaises(ModelAdapterError):
                build_profile_model(key, config())

    def test_legacy_payload_is_unchanged_unless_bound_is_explicit(self):
        for model_type in (OpenAICompatibleModel, AsyncOpenAICompatibleModel):
            old = model_type(KEY, "fixture")
            self.assertNotIn("max_tokens", old._payload(REQUEST)[0])
            bounded = model_type(KEY, "fixture", max_output_tokens=23)
            self.assertEqual(bounded._payload(REQUEST)[0]["max_tokens"], 23)
            self.assertEqual(bounded._payload(REQUEST, stream=True)[0]["max_tokens"], 23)


class ProfileAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_auth_bound_and_client_closed_for_both_modes(self):
        model = build_profile_model(KEY, config(max_output_tokens=19))
        streams = []
        def respond(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["max_tokens"], 19)
            self.assertEqual(request.headers["authorization"], "Bearer " + KEY)
            self.assertEqual(str(request.url), "http://127.0.0.1:12345/chat/completions")
            self.assertNotIn("cookie", request.headers)
            body = [frame(chunk({"content": "OK"}, "stop", USAGE)), frame("[DONE]")] if payload["stream"] else [json.dumps(completion("OK")).encode()]
            stream = Pieces(body)
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "text/event-stream" if payload["stream"] else "application/json"}, stream=stream)
        clients, transports = attach(model, respond)
        self.assertEqual((await model.complete(REQUEST)).text, "OK")
        self.assertEqual((await model.stream(REQUEST, lambda value: None)).text, "OK")
        self.assertEqual(sum(len(t.requests) for t in transports), 2)
        self.assertTrue(all(s.closed for s in streams) and all(c.is_closed for c in clients))

    async def test_dns_private_mixed_and_failure_dispatch_zero_requests(self):
        model = build_profile_model(KEY, config("https://models.example.com/v1"))
        _, transports = attach(model, lambda _: json_response())
        loop = asyncio.get_running_loop()
        good = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        bad = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        for answers in ([bad], [good, bad], [], [good] * 33):
            with self.subTest(answers=len(answers)), patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=answers)):
                with self.assertRaisesRegex(ModelAdapterError, "^model_endpoint_not_public$"):
                    await model.complete(REQUEST)
                self.assertFalse(model.request_issued)
        with patch.object(loop, "getaddrinfo", new=AsyncMock(side_effect=socket.gaierror("private resolver detail"))):
            with self.assertRaisesRegex(ModelAdapterError, "^model_dns_failed$"):
                await model.complete(REQUEST)
        self.assertEqual(transports, [])
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=[good])):
            self.assertEqual((await model.complete(REQUEST)).text, "ok")
        self.assertEqual(len(transports), 1)

    async def test_http_errors_are_fixed_single_attempt_and_close(self):
        for status, code in ((301, "model_transport_error"), (302, "model_transport_error"),
                             (401, "model_authentication_failed"), (403, "model_authentication_failed"),
                             (429, "model_rate_limited"), (500, "model_transport_error")):
            with self.subTest(status=status):
                model = build_profile_model(KEY, config())
                stream = Pieces([("private-provider-body:" + KEY).encode()])
                clients, transports = attach(model, lambda _: json_response(status=status, stream=stream,
                    headers={"content-type": "application/json", "location": "http://127.0.0.1:1/stolen"}))
                with self.assertRaisesRegex(ModelAdapterError, "^" + code + "$"):
                    await model.complete(REQUEST)
                self.assertEqual(sum(len(t.requests) for t in transports), 1)
                self.assertEqual(stream.yielded, 0)
                self.assertTrue(stream.closed and clients[0].is_closed)

    async def test_response_headers_body_line_and_request_are_bounded(self):
        cases = [({"content-type": "application/json", "content-encoding": "gzip"}, Pieces([b"secret"]), "model_protocol_error"),
                 ({"content-type": "application/json", "content-length": "9999999"}, Pieces([b"secret"]), "model_response_too_large"),
                 ({"content-type": "application/json", "x-large": "x" * 17000}, Pieces([b"secret"]), "model_response_too_large"),
                 ({"content-type": "application/json"}, Pieces([b"x" * 200]), "model_response_too_large")]
        for headers, stream, code in cases:
            model = AsyncProfileModel(KEY, config(), max_response_bytes=128)
            clients, _ = attach(model, lambda _: json_response(headers=headers, stream=stream))
            with self.assertRaisesRegex(ModelAdapterError, "^" + code + "$"):
                await model.complete(REQUEST)
            self.assertTrue(stream.closed and clients[0].is_closed)
        model = build_profile_model(KEY, config())
        stream = Pieces([b"data: " + b"x" * 262145])
        attach(model, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))
        with self.assertRaisesRegex(ModelAdapterError, "^model_response_too_large$"):
            await model.stream(REQUEST, lambda value: None)
        self.assertTrue(stream.closed)
        oversized = ModelRequest((Message("user", "x" * 1_000_001),), ())
        _, transports = attach(model, lambda _: json_response())
        with self.assertRaisesRegex(ModelAdapterError, "^model_request_too_large$"):
            await model.complete(oversized)
        self.assertEqual(transports, [])

    async def test_total_timeout_stops_slow_active_stream_and_cancel_closes(self):
        class Slow(Pieces):
            async def __aiter__(self):
                while True:
                    yield b": heartbeat\n\n"
                    await asyncio.sleep(.1)
        model = build_profile_model(KEY, config(timeout=1))
        stream = Slow([])
        clients, _ = attach(model, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))
        start = asyncio.get_running_loop().time()
        with self.assertRaisesRegex(ModelAdapterError, "^model_timeout$"):
            await model.stream(REQUEST, lambda value: None)
        self.assertLess(asyncio.get_running_loop().time() - start, 2)
        self.assertTrue(stream.closed and clients[0].is_closed)
        gate = asyncio.Event()
        stream = Pieces([b": start\n\n"], wait_after_first=gate)
        attach(model, lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))
        task = asyncio.create_task(model.stream(REQUEST, lambda value: None))
        while not stream.yielded:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)

    async def test_real_local_connection_probe_and_no_env_proxy(self):
        async def respond(reader, writer, request):
            body = json.dumps(completion("OK")).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        async with local_http(respond) as (url, requests):
            with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                                           "SSL_CERT_FILE": "not-a-real-certificate"}):
                result = await probe_profile(KEY, config(url, max_output_tokens=512, timeout=30))
        self.assertTrue(result["ok"] and result["request_issued"])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["body"]["max_tokens"], 32)
        self.assertNotIn("tools", requests[0]["body"])
        self.assertEqual(requests[0]["body"]["messages"], [{"role": "user", "content": "Reply with OK."}])

    async def test_probe_missing_key_zero_requests_and_error_redaction(self):
        with patch("httpx.AsyncClient", side_effect=AssertionError("missing key should not request")):
            result = await probe_profile("", config())
        self.assertEqual(result["error_code"], "model_credentials_missing")
        self.assertFalse(result["request_issued"])
        with patch.object(AsyncProfileModel, "complete", new=AsyncMock(side_effect=ModelAdapterError(KEY))):
            result = await probe_profile(KEY, config())
        self.assertEqual(result["error_code"], "model_transport_error")
        self.assertNotIn(KEY, json.dumps(result))

    async def test_bound_probe_reuses_frozen_key_and_does_not_change_task_limits(self):
        model = build_profile_model(KEY, config(max_output_tokens=2000, timeout=60))
        with patch("astra_harness.builtin.model_profile_adapter.test_profile_model", new=AsyncMock(return_value={"ok": True})) as probe:
            with patch.dict("os.environ", {"ASTRA_MODEL_API_KEY": "different-synthetic-key"}):
                self.assertTrue((await probe_model(model))["ok"])
        probe.assert_awaited_once_with(KEY, dict(model.profile_config))
        self.assertEqual((model.timeout, model.max_output_tokens, model.max_response_bytes), (60, 2000, 2_000_000))


if __name__ == "__main__":
    unittest.main()
