"""Protocol and outbound-boundary checks; vectors are manually assigned fixtures."""
import asyncio
from dataclasses import FrozenInstanceError
import json
import os
import time
import unittest
from unittest.mock import patch

from astra_harness.builtin.embeddings import (
    DEFAULT_KEY_ENV, EmbeddingConfig, EmbeddingError, OpenAICompatibleEmbeddingProvider,
    parse_embedding_config, validate_embedding_endpoint,
)
from examples.embedding_mock_server import MockEmbeddingServer


def configured(server, **changes):
    values = {"enabled": True, "endpoint": server.url, "model": "fixture-embedding",
              "dimensions": 3, "timeout": 2}
    values.update(changes)
    return EmbeddingConfig(**values)


def provider(server, **changes):
    config = configured(server, **changes)
    return OpenAICompatibleEmbeddingProvider(config, environ={config.key_env: "mock-embedding-key"})


class EmbeddingConfigurationTests(unittest.TestCase):
    def test_default_is_disabled_and_roundtrip_is_stable(self):
        config = parse_embedding_config(None)
        self.assertFalse(config.enabled)
        self.assertEqual(parse_embedding_config(config.public_config()), config)
        self.assertEqual(config.identity(), parse_embedding_config(dict(reversed(list(config.public_config().items())))).identity())
        with self.assertRaises(FrozenInstanceError):
            config.model = "changed"

    def test_invalid_fields_cannot_include_raw_credentials_or_chat_key_reference(self):
        invalid = [{"api_key": "fixture-secret"}, {"key_env": "ASTRA_API_KEY"},
                   {"enabled": 1}, {"enabled": True}, {"model": "space model"},
                   {"dimensions": True}, {"dimensions": 0}, {"dimensions": 4097},
                   {"timeout": float("nan")}, {"timeout": 0}, {"timeout": 61},
                   {"revision": "a"*65}, {"revision": "bad revision"}]
        for value in invalid:
            with self.subTest(fields=list(value)), self.assertRaisesRegex(ValueError, "^invalid_embedding_config$"):
                parse_embedding_config(value)
        self.assertEqual(parse_embedding_config({"key_env": "ASTRA_EMBEDDING_LOCAL_API_KEY"}).key_env,
                         "ASTRA_EMBEDDING_LOCAL_API_KEY")

    def test_endpoint_policy_rejects_ambiguous_or_credential_bearing_urls(self):
        for endpoint in (
            "http://localhost:42/v1/embeddings", "http://127.0.0.1/v1/embeddings",
            "http://10.1.2.3:42/v1/embeddings", "https://127.0.0.1/v1/embeddings",
            "https://127.1/v1/embeddings", "https://0177.0.0.1/v1/embeddings",
            "https://service.internal/v1/embeddings", "https://user:secret@api.example.com/v1/embeddings",
            "https://api.example.com/v1/embeddings?key=secret", "https://api.example.com/v1/embeddings#x",
            "https://api.example.com/v1/../embeddings", "https://api.example.com/%65mbeddings",
            "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/embeddings\n",
            "https://api.example.com:0/v1/embeddings", "https://api.example.com:65536/v1/embeddings",
        ):
            with self.subTest(kind=endpoint.split(":", 1)[0]), self.assertRaisesRegex(ValueError, "^invalid_embedding_endpoint$"):
                validate_embedding_endpoint(endpoint)
        self.assertEqual(validate_embedding_endpoint("https://API.EXAMPLE.COM:443/v1/embeddings"),
                         "https://api.example.com/v1/embeddings")
        self.assertEqual(validate_embedding_endpoint("http://[::1]:12345/v1/embeddings"),
                         "http://[::1]:12345/v1/embeddings")

    def test_configuration_changes_are_bound_but_key_is_not_index_identity(self):
        config = EmbeddingConfig(enabled=True, endpoint="https://api.example.com/v1/embeddings", model="m")
        baseline = config.identity()
        for changes in ({"model": "n"}, {"dimensions": 3}, {"revision": "2"}, {"timeout": 3},
                        {"endpoint": "https://other.example.com/v1/embeddings"}, {"enabled": False}):
            self.assertNotEqual(parse_embedding_config({**config.public_config(), **changes}).identity(), baseline)
        first = OpenAICompatibleEmbeddingProvider(config, environ={DEFAULT_KEY_ENV: "fixture-key-a"})
        same = OpenAICompatibleEmbeddingProvider(config, environ={DEFAULT_KEY_ENV: "fixture-key-a"})
        changed = OpenAICompatibleEmbeddingProvider(config, environ={DEFAULT_KEY_ENV: "fixture-key-b"})
        self.assertEqual(first.recovery_identity(), same.recovery_identity())
        self.assertNotEqual(first.recovery_identity(), changed.recovery_identity())
        self.assertEqual(first.config.identity(), changed.config.identity())
        self.assertNotIn("fixture-key", repr(first) + repr(config) + json.dumps(first.status()))

    def test_fresh_clients_work_across_caller_event_loops_and_never_forward_cookies(self):
        with MockEmbeddingServer() as server:
            instance = provider(server)
            asyncio.run(instance.embed(["one"]))
            asyncio.run(instance.embed(["two"]))
            self.assertEqual(len(server.requests), 2)
            self.assertTrue(all(row["auth_ok"] for row in server.requests))
            self.assertFalse(any(row["cookie_present"] for row in server.requests))


class EmbeddingProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_missing_or_invalid_key_never_constructs_a_client(self):
        with patch("astra_harness.builtin.embeddings.httpx.AsyncClient") as client:
            configurations = [
                (EmbeddingConfig(), {DEFAULT_KEY_ENV: "a-key"}, "embedding_not_configured"),
                (EmbeddingConfig(enabled=True, endpoint="https://api.example.com/v1/embeddings", model="m"),
                 {"ASTRA_API_KEY": "chat-key-must-not-be-reused"}, "embedding_missing_key"),
                (EmbeddingConfig(enabled=True, endpoint="https://api.example.com/v1/embeddings", model="m"),
                 {DEFAULT_KEY_ENV: "invalid\nheader"}, "embedding_invalid_key"),
            ]
            for config, environment, expected in configurations:
                instance = OpenAICompatibleEmbeddingProvider(config, environ=environment)
                with self.assertRaises(EmbeddingError) as error:
                    await instance.embed(["synthetic input"])
                self.assertEqual(error.exception.code, expected)
                self.assertFalse(error.exception.request_issued)
            client.assert_not_called()

    async def test_input_validation_happens_before_network(self):
        with MockEmbeddingServer() as server:
            instance = provider(server)
            for texts in ("string", [], ["x"] * 9, [""], [" "], ["x"*1001], ["\0"], ["\ud800"], [1]):
                with self.subTest(type=str(type(texts))), self.assertRaises(EmbeddingError) as error:
                    await instance.embed(texts)
                self.assertEqual(error.exception.code, "embedding_invalid_input")
                self.assertFalse(error.exception.request_issued)
            self.assertEqual(server.requests, [])

    async def test_reverse_response_order_is_reconstructed_by_index(self):
        with MockEmbeddingServer(mode="reverse", vectors_by_text={"fruit": [1, 0, 0], "sea": [0, 1, 0]}) as server:
            result = await provider(server).embed(["fruit", "sea"])
            self.assertEqual(result.vectors, ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))
            self.assertEqual(result.model, "fixture-embedding")
            self.assertEqual(result.usage, {"prompt_tokens": 2, "total_tokens": 2})
            self.assertEqual(server.requests[0]["dimensions"], 3)
            self.assertEqual(server.requests[0]["encoding_format"], "float")

    async def test_dimensions_option_is_not_sent_unless_selected(self):
        with MockEmbeddingServer() as server:
            result = await provider(server, dimensions=None).embed(["text"])
            self.assertEqual(len(result.vectors[0]), 3)
            self.assertIsNone(server.requests[0]["dimensions"])

    async def test_response_invariants_are_rejected_with_no_retry(self):
        cases = {"missing": "embedding_protocol_error", "duplicate": "embedding_protocol_error",
                 "zero": "embedding_protocol_error", "nonfinite": "embedding_protocol_error",
                 "model_mismatch": "embedding_model_mismatch", "wrong_dimension": "embedding_dimension_mismatch",
                 "invalid_json": "embedding_protocol_error", "gzip": "embedding_protocol_error"}
        for mode, expected in cases.items():
            with self.subTest(mode=mode), MockEmbeddingServer(mode=mode) as server:
                with self.assertRaises(EmbeddingError) as error:
                    await provider(server).embed(["one", "two"])
                self.assertEqual(error.exception.code, expected)
                self.assertTrue(error.exception.request_issued)
                self.assertEqual(len(server.requests), 1)
                self.assertNotIn("fixture-secret", str(error.exception))

    async def test_provider_errors_are_fixed_codes_and_do_not_echo_response(self):
        for mode, expected in (("error", "embedding_provider_error"), ("auth_error", "embedding_authentication_failed"),
                               ("rate_limit", "embedding_rate_limited")):
            with self.subTest(mode=mode), MockEmbeddingServer(mode=mode) as server:
                with self.assertRaises(EmbeddingError) as error:
                    await provider(server).embed(["private synthetic text"])
                self.assertEqual(str(error.exception), expected)
                self.assertTrue(error.exception.request_issued)
                self.assertNotIn("private", repr(error.exception))
                self.assertNotIn("mock-embedding-key", repr(server.requests))
                self.assertEqual(len(server.requests), 1)

    async def test_environment_proxy_and_server_redirect_are_not_followed(self):
        with MockEmbeddingServer() as trap, MockEmbeddingServer(mode="redirect") as server:
            proxy = trap.url.removesuffix("/v1/embeddings")
            with patch.dict(os.environ, {"HTTP_PROXY": proxy, "ALL_PROXY": proxy, "NO_PROXY": ""}):
                with self.assertRaises(EmbeddingError):
                    await provider(server).embed(["one"])
            self.assertEqual(trap.paths, [])
            self.assertEqual(server.paths, ["/v1/embeddings"])

    async def test_both_declared_and_streamed_response_limits_are_enforced(self):
        for mode in ("oversized", "oversized_stream"):
            with self.subTest(mode=mode), MockEmbeddingServer(mode=mode) as server:
                with self.assertRaises(EmbeddingError) as error:
                    await provider(server).embed(["one"])
                self.assertEqual(error.exception.code, "embedding_response_limit")
                self.assertTrue(error.exception.request_issued)
                self.assertEqual(len(server.requests), 1)

    async def test_total_deadline_stops_a_response_that_keeps_trickling(self):
        with MockEmbeddingServer(mode="slow") as server:
            start = time.monotonic()
            with self.assertRaises(EmbeddingError) as error:
                await provider(server, timeout=.15).embed(["one"])
            self.assertEqual(error.exception.code, "embedding_timeout")
            self.assertTrue(error.exception.request_issued)
            self.assertLess(time.monotonic()-start, 1)
            self.assertEqual(len(server.requests), 1)

    async def test_cancellation_does_not_resubmit_or_wrap_a_provider_error(self):
        with MockEmbeddingServer(mode="delayed") as server:
            task = asyncio.create_task(provider(server).embed(["one"]))
            deadline = time.monotonic()+2
            while not server.received.is_set():
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(.03)
            self.assertEqual(len(server.requests), 1)

    async def test_credential_is_frozen_when_provider_is_built(self):
        with MockEmbeddingServer() as server:
            config = configured(server)
            environment = {config.key_env: "mock-embedding-key"}
            instance = OpenAICompatibleEmbeddingProvider(config, environ=environment)
            identity = instance.recovery_identity()
            environment[config.key_env] = "different-key"
            await instance.embed(["one"])
            self.assertTrue(server.requests[0]["auth_ok"])
            self.assertEqual(instance.recovery_identity(), identity)
            self.assertNotEqual(OpenAICompatibleEmbeddingProvider(config, environ=environment).recovery_identity(), identity)


if __name__ == "__main__":
    unittest.main()
