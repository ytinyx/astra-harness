"""HTTPX 假传输与真实回环 HTTP；不读取凭据或调用外部供应商。"""

import asyncio
from contextlib import asynccontextmanager
import inspect
import json
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

import httpx

from astra_harness.application import Application
from astra_harness.builtin.async_models import AsyncOpenAICompatibleModel
from astra_harness.builtin.models import ModelAdapterError, OpenAICompatibleModel
from astra_harness.contracts import Message, ModelRequest, ToolCall, ToolSpec
from astra_harness.engine import Engine
from astra_harness.tools import Tool


REQUEST = ModelRequest((Message("user", "你好"),), ())
USAGE = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}


def frame(value, ending=b"\r\n"):
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return b"data: " + value.encode("utf-8") + ending + ending


def chunk(delta=None, finish=None, usage=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], "usage": usage}


def completion(text="ok", finish="stop", calls=None, usage=USAGE):
    message = {"content": text}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}


class Pieces(httpx.AsyncByteStream):
    def __init__(self, pieces, *, wait_after_first=None, failure=None):
        self.pieces = pieces
        self.wait_after_first = wait_after_first
        self.failure = failure
        self.closed = False
        self.yielded = 0

    async def __aiter__(self):
        for piece in self.pieces:
            self.yielded += 1
            yield piece
            if self.yielded == 1 and self.wait_after_first is not None:
                await self.wait_after_first.wait()
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, respond):
        self.respond, self.requests, self.closed = respond, [], False

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.respond(request)

    async def aclose(self):
        self.closed = True


def fake_model(respond, **kwargs):
    model = AsyncOpenAICompatibleModel("test-only-not-a-key", "test-model", **kwargs)
    clients, transports = [], []

    def client():
        transport = RecordingTransport(respond)
        result = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
        transports.append(transport)
        clients.append(result)
        return result

    model._client = client
    return model, clients, transports


def response(pieces, *, status=200, headers=None):
    stream = pieces if isinstance(pieces, Pieces) else Pieces(pieces)
    return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, stream=stream)


@asynccontextmanager
async def local_server(respond):
    requests, tasks = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            headers = dict(line.split(":", 1) for line in head.decode().split("\r\n")[1:] if ":" in line)
            length = int(next(value for name, value in headers.items() if name.lower() == "content-length"))
            payload = json.loads(await reader.readexactly(length))
            requests.append(payload)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await respond(reader, writer, payload)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


class AsyncModelTests(unittest.IsolatedAsyncioTestCase):
    def assert_closed(self, clients, transports, stream):
        self.assertTrue(stream.closed)
        self.assertTrue(all(client.is_closed for client in clients))
        self.assertTrue(all(transport.closed for transport in transports))

    async def test_type_configuration_payload_and_transport_policy(self):
        stream = Pieces([json.dumps(completion()).encode()])
        transport = RecordingTransport(lambda _: response(stream))
        real_client = httpx.AsyncClient
        clients = []

        def client(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            result = real_client(**kwargs)
            clients.append(result)
            return result

        model = AsyncOpenAICompatibleModel("test-only-not-a-key", "deepseek-flash")
        self.assertIsInstance(model, OpenAICompatibleModel)
        self.assertTrue(inspect.iscoroutinefunction(model.complete))
        self.assertTrue(inspect.iscoroutinefunction(model.stream))
        with patch("astra_harness.builtin.async_models.httpx.AsyncHTTPTransport", return_value=transport) as factory, \
             patch("astra_harness.builtin.async_models.httpx.AsyncClient", side_effect=client):
            self.assertEqual((await model.complete(REQUEST)).text, "ok")
        factory.assert_called_once_with(retries=0, trust_env=False)
        request = transport.requests[0]
        self.assertEqual(request.headers['accept-encoding'], 'identity')
        payload = json.loads(request.content)
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertFalse(payload['stream'])
        self.assert_closed(clients, [transport], stream)
        for url in ("http://remote.example", "https://user:password@example.test", "https://example.test?q=x"):
            with self.assertRaises(ValueError):
                AsyncOpenAICompatibleModel("fake", "fake", url)
        for limit in (float('inf'), float('nan'), 2.5, True, 0, -1, None):
            with self.assertRaisesRegex(ValueError, '^invalid_model_configuration$'):
                AsyncOpenAICompatibleModel("fake", "fake", max_response_bytes=limit)

    async def test_complete_tool_name_roundtrip_and_usage(self):
        spec = ToolSpec("files.read", "read", (), {"type": "object", "properties": {}})
        request = ModelRequest(REQUEST.messages, (spec,))
        streams = []

        def respond(incoming):
            payload = json.loads(incoming.content)
            name = payload['tools'][0]['function']['name']
            data = completion(None, 'tool_calls', [{'id': 'c1', 'function': {'name': name, 'arguments': '{"path":"会议.md"}'}}])
            stream = Pieces([json.dumps(data, ensure_ascii=False).encode()])
            streams.append(stream)
            return response(stream)

        model, clients, transports = fake_model(respond)
        result = await model.complete(request)
        self.assertEqual(result.tool_calls, (ToolCall('c1', 'files.read', {'path': '会议.md'}),))
        self.assertEqual(result.usage, {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13})
        self.assert_closed(clients, transports, streams[0])

    async def test_complete_rejects_incomplete_or_invalid_response(self):
        cases = [completion(finish=reason) for reason in (None, 'length', 'content_filter', 'aborted')]
        cases += [completion(finish='tool_calls'), completion(text=False), completion(usage={**USAGE, 'total_tokens': True}),
                  {'choices': []}, {'error': 'private-provider-error', **completion()},
                  completion(finish='tool_calls', calls=[{'id': 'c', 'type': 'custom', 'function': {'name': 'add', 'arguments': '{}'}}]),
                  completion(calls=[{'id': 'c', 'function': {'name': 'add', 'arguments': '{}'}}])]
        raw_cases = [json.dumps(value).encode() for value in cases]
        raw_cases.append(b'{"choices":[],"choices":[]}')
        for raw in raw_cases:
            with self.subTest(raw=raw[:30]):
                stream = Pieces([raw])
                model, clients, transports = fake_model(lambda _: response(stream))
                with self.assertRaisesRegex(ModelAdapterError, '^model_protocol_error$'):
                    await model.complete(REQUEST)
                self.assert_closed(clients, transports, stream)

    async def test_utf8_and_all_sse_line_endings_across_byte_chunks(self):
        for ending in (b'\n', b'\r\n', b'\r'):
            with self.subTest(ending=ending):
                body = b'\xef\xbb\xbf: heartbeat' + ending + ending
                body += frame(chunk({'content': '你好'}, 'stop', USAGE), ending) + frame('[DONE]', ending)
                stream = Pieces([body[index:index+1] for index in range(len(body))])
                model, clients, transports = fake_model(lambda _: response(stream))
                deltas = []
                result = await model.stream(REQUEST, deltas.append)
                self.assertEqual((result.text, deltas), ('你好', ['你好']))
                self.assert_closed(clients, transports, stream)

    async def test_stream_tool_fragments_missing_usage_and_multiline_event(self):
        spec = ToolSpec("files.read", "read", (), {"type": "object", "properties": {}})
        request = ModelRequest(REQUEST.messages, (spec,))
        streams = []

        def respond(incoming):
            name = json.loads(incoming.content)['tools'][0]['function']['name']
            body = [frame(chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': name[:8], 'arguments': '{"path":'}}]})),
                    frame(chunk({'tool_calls': [{'index': 0, 'id': '1', 'function': {'name': name[8:], 'arguments': '"x.md"}'}}]}, 'tool_calls')),
                    frame('[DONE]')]
            stream = Pieces(body)
            streams.append(stream)
            return response(stream)

        model, clients, transports = fake_model(respond)
        result = await model.stream(request, lambda _: self.fail('tool fragments are not display text'))
        self.assertEqual(result.tool_calls, (ToolCall('c1', 'files.read', {'path': 'x.md'}),))
        self.assertIsNone(result.usage)
        self.assert_closed(clients, transports, streams[0])
        multiline = b'data: {"choices":\ndata: [{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        model, _, _ = fake_model(lambda _: response([multiline, frame('[DONE]')]))
        self.assertEqual((await model.stream(REQUEST, lambda _: None)).text, 'ok')

    async def test_stream_rejects_partial_calls_bad_frames_and_nonfinal_usage(self):
        good = frame(chunk({'content': 'ok'}, 'stop'))
        usage = frame({'choices': [], 'usage': USAGE})
        cases = [[frame('[DONE]')], [good], [good, b'data: [DONE]'], [good, frame(chunk({'content': 'late'})), frame('[DONE]')],
                 [frame(chunk({'content': 'partial'}, 'length')), frame('[DONE]')],
                 [frame(chunk({'content': 'partial'}, usage=USAGE)), good, frame('[DONE]')],
                 [good, usage, usage, frame('[DONE]')], [usage, good, frame('[DONE]')],
                 [b'data: {"choices":[],"choices":[]}\n\n', frame('[DONE]')],
                 [frame(chunk({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'add', 'arguments': '{"a":'}}]}, 'tool_calls')), frame('[DONE]')],
                 [frame(chunk({'tool_calls': [{'index': True, 'id': 'c1', 'function': {'name': 'add', 'arguments': '{}'}}]}, 'tool_calls')), frame('[DONE]')],
                 [frame({'choices': [{'index': False, 'delta': {}, 'finish_reason': 'stop'}]}), frame('[DONE]')],
                 [b'data: \xff\n\n', frame('[DONE]')]]
        for index, body in enumerate(cases):
            with self.subTest(index=index):
                stream = Pieces(body)
                model, clients, transports = fake_model(lambda _: response(stream))
                with self.assertRaisesRegex(ModelAdapterError, '^model_protocol_error$'):
                    await model.stream(REQUEST, lambda _: None)
                self.assert_closed(clients, transports, stream)

    async def test_response_and_single_line_limits_apply_before_unbounded_buffering(self):
        for method, body, limit in (('complete', [b' ' * 101], 100), ('stream', [b':' * 262145], 300000),
                                    ('stream', [b':' * 60, b' ' * 60], 100)):
            with self.subTest(method=method, limit=limit):
                stream = Pieces(body)
                model, clients, transports = fake_model(lambda _: response(stream), max_response_bytes=limit)
                with self.assertRaisesRegex(ModelAdapterError, '^model_response_too_large$'):
                    await (model.complete(REQUEST) if method == 'complete' else model.stream(REQUEST, lambda _: None))
                self.assert_closed(clients, transports, stream)

    async def test_compression_and_wrong_stream_content_type_are_rejected(self):
        for headers in ({'content-type': 'text/event-stream', 'content-encoding': 'gzip'}, {'content-type': 'application/json'}):
            stream = Pieces([frame(chunk({'content': 'ok'}, 'stop')), frame('[DONE]')])
            model, clients, transports = fake_model(lambda _: response(stream, headers=headers))
            with self.assertRaisesRegex(ModelAdapterError, '^model_protocol_error$'):
                await model.stream(REQUEST, lambda _: None)
            self.assertEqual(stream.yielded, 0)
            self.assert_closed(clients, transports, stream)

    async def test_callback_stop_closes_resources(self):
        stream = Pieces([frame(chunk({'content': 'visible'})), frame(chunk({}, 'stop')), frame('[DONE]')])
        model, clients, transports = fake_model(lambda _: response(stream))
        with self.assertRaisesRegex(ModelAdapterError, '^model_stream_closed$'):
            await model.stream(REQUEST, lambda _: False)
        self.assertEqual(stream.yielded, 1)
        self.assert_closed(clients, transports, stream)

    async def test_http_errors_and_redirects_do_not_retry_or_expose_body(self):
        for status in (301, 307, 401, 429, 503):
            stream = Pieces([b'private-provider-body'])
            model, clients, transports = fake_model(lambda _: response(stream, status=status, headers={'location': 'https://elsewhere.invalid'}))
            with self.assertRaisesRegex(ModelAdapterError, '^model_transport_error$'):
                await model.complete(REQUEST)
            self.assertEqual(sum(len(t.requests) for t in transports), 1)
            self.assertEqual(stream.yielded, 0)
            self.assert_closed(clients, transports, stream)

    async def test_transport_failure_is_sanitized_and_closes_client(self):
        stream = Pieces([], failure=httpx.ReadTimeout('private-body-and-key-marker'))
        model, clients, transports = fake_model(lambda _: response(stream))
        try:
            await model.complete(REQUEST)
        except ModelAdapterError as error:
            self.assertEqual(str(error), 'model_transport_error')
            self.assertNotIn('private-body-and-key-marker', ''.join(traceback.format_exception(error)))
        else:
            self.fail('must reject transport failure')
        self.assert_closed(clients, transports, stream)

    async def test_cancellation_propagates_and_closes_complete_and_stream(self):
        for streaming in (False, True):
            release = asyncio.Event()
            body = frame(chunk({'content': 'partial'})) if streaming else b'{'
            stream = Pieces([body], wait_after_first=release)
            model, clients, transports = fake_model(lambda _: response(stream))
            task = asyncio.create_task(model.stream(REQUEST, lambda _: None) if streaming else model.complete(REQUEST))
            while stream.yielded == 0:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assert_closed(clients, transports, stream)

    async def test_real_http_delivers_small_delta_before_completion_and_ignores_proxy_env(self):
        received, release = asyncio.Event(), asyncio.Event()

        async def respond(reader, writer, payload):
            first = frame(chunk({'content': '你好'}))
            for value in first:
                writer.write(bytes([value]))
                await writer.drain()
            await release.wait()
            writer.write(frame(chunk({'content': '世界'}, 'stop')) + frame({'choices': [], 'usage': USAGE}) + frame('[DONE]'))
            await writer.drain()

        async with local_server(respond) as (url, requests):
            model = AsyncOpenAICompatibleModel('local-test-only', 'local-model', url)
            deltas = []
            def delta(text):
                deltas.append(text)
                received.set()
            with patch.dict('os.environ', {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                task = asyncio.create_task(model.stream(REQUEST, delta))
                try:
                    await asyncio.wait_for(received.wait(), 2)
                    self.assertFalse(task.done())
                    self.assertEqual(deltas, ['你好'])
                finally:
                    release.set()
                result = await asyncio.wait_for(task, 2)
            self.assertEqual(result.text, '你好世界')
            self.assertEqual(result.usage['total_tokens'], 13)
            self.assertEqual(len(requests), 1)

    async def test_real_http_read_timeout_closes_connection(self):
        disconnected = asyncio.Event()
        async def respond(reader, writer, payload):
            writer.write(frame(chunk({'content': 'partial'})))
            await writer.drain()
            if await reader.read(1) == b'':
                disconnected.set()
        async with local_server(respond) as (url, _):
            model = AsyncOpenAICompatibleModel('local-test-only', 'local-model', url, timeout=.05)
            with self.assertRaisesRegex(ModelAdapterError, '^model_transport_error$'):
                await model.stream(REQUEST, lambda _: None)
            await asyncio.wait_for(disconnected.wait(), 2)

    async def test_engine_timeout_and_cancel_close_async_http_without_late_tool_execution(self):
        for cancel_run in (False, True):
            entered, disconnected = asyncio.Event(), asyncio.Event()
            async def respond(reader, writer, payload):
                writer.write(frame(chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'add', 'arguments': '{"a":'}}]})))
                await writer.drain()
                entered.set()
                if await reader.read(1) == b'':
                    disconnected.set()
            async with local_server(respond) as (url, _):
                model = AsyncOpenAICompatibleModel('local-test-only', 'local-model', url, timeout=4)
                cancelled, executed = threading.Event(), []
                engine = Engine(model, (Tool('add', 'sum', ('a', 'b'), lambda a, b: executed.append(a+b)),))
                # Allow cold certificate/client startup before timing out the stalled body.
                state = engine.create('add', limits={'model_timeout': 3 if cancel_run else .75})
                task = asyncio.create_task(engine.advance(state, cancelled=cancelled.is_set))
                await asyncio.wait_for(entered.wait(), 2)
                if cancel_run:
                    cancelled.set()
                await asyncio.wait_for(task, 2)
                await asyncio.wait_for(disconnected.wait(), 2)
                self.assertEqual(state['status'], 'cancelled' if cancel_run else 'failed')
                self.assertEqual(state['error'], 'cancelled' if cancel_run else 'model_timeout')
                self.assertEqual((state['pending'], executed), ([], []))

    async def test_engine_uses_async_adapter_for_complete_tool_roundtrip(self):
        streams = []
        def respond(request):
            payload = json.loads(request.content)
            if payload['messages'][-1]['role'] == 'tool':
                data = chunk({'content': '5'}, 'stop', USAGE)
            else:
                data = chunk({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'add', 'arguments': '{"a":2,"b":3}'}}]}, 'tool_calls', USAGE)
            stream = Pieces([frame(data), frame('[DONE]')])
            streams.append(stream)
            return response(stream)
        model, clients, transports = fake_model(respond)
        executed = []
        engine = Engine(model, (Tool('add', 'sum', ('a', 'b'), lambda a, b: executed.append(a+b) or a+b),))
        state = engine.create('2+3')
        for _ in range(3):
            await engine.advance(state)
        self.assertEqual((state['status'], state['output'], executed), ('completed', '5', [5]))
        self.assertEqual(state['usage']['reported_calls'], 2)
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertTrue(all(client.is_closed for client in clients))

    async def test_adapter_class_change_refuses_old_pending_run(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'runs.sqlite3'
            first = OpenAICompatibleModel('fake', 'same-model', 'https://example.invalid')
            second = AsyncOpenAICompatibleModel('fake', 'same-model', 'https://example.invalid')
            with Application(first, database=database, autostart=False) as app:
                submitted = app.submit('pending')
            with Application(second, database=database, autostart=False) as app:
                state = app.get_run(submitted['id'])
                self.assertEqual((state['status'], state['error']), ('failed', 'configuration_changed'))


if __name__ == '__main__':
    unittest.main()
