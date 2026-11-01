"""以实际请求和处理函数调用次数验证循环，而不是只检查最终文案。"""

import unittest

from astra_harness import Harness, ModelResponse, RunLimits, Tool, ToolCall


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        response = next(self.responses)
        return response(request) if callable(response) else response


def response(*calls):
    return ModelResponse(tool_calls=tuple(calls))


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.executed = []

        def add(a, b):
            self.executed.append((a, b))
            return a + b

        self.add = Tool("add", "加法", ("a", "b"), add)
        self.call = ToolCall("c1", "add", {"a": 2, "b": 3})

    def test_direct_answer_has_no_tool_execution(self):
        model = ScriptedModel(ModelResponse(text="你好"))
        result = Harness(model, (self.add,)).run("你好")
        self.assertEqual((result.reason, result.model_calls, result.tool_calls), ("completed", 1, 0))
        self.assertEqual(result.output, "你好")
        self.assertEqual(self.executed, [])

    def test_two_rounds_preserve_call_and_real_result(self):
        def final_answer(request):
            result = request.messages[-1].tool_result
            self.assertEqual(request.messages[-2].tool_calls, (self.call,))
            self.assertEqual((result.call_id, result.name, result.value), ("c1", "add", 5))
            return ModelResponse(text=str(result.value))

        model = ScriptedModel(response(self.call), final_answer)
        result = Harness(model, (self.add,)).run("2 + 3", limits=RunLimits(2, 1))
        self.assertEqual((result.output, result.model_calls, result.tool_calls), ("5", 2, 1))
        self.assertEqual(self.executed, [(2, 3)])
        self.assertEqual(len(model.requests[0].messages), 1)
        schema = model.requests[0].tools[0].schema()
        self.assertEqual(schema["required"], ["a", "b"])
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(hasattr(model.requests[0].tools[0], "handler"))

    def test_unknown_tool_is_paired_and_not_executed(self):
        call = ToolCall("c1", "missing", {})
        model = ScriptedModel(response(call), ModelResponse(text="无法执行"))
        result = Harness(model, (self.add,)).run("调用")
        tool_result = model.requests[1].messages[-1].tool_result
        self.assertEqual((tool_result.call_id, tool_result.error_code), ("c1", "unknown_tool"))
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(self.executed, [])

    def test_invalid_arguments_never_enter_handler(self):
        cases = [
            {"a": 2}, {"a": 2, "b": 3, "extra": 4}, {"a": "2", "b": 3},
            {"a": True, "b": 3}, {"a": float("nan"), "b": 3},
            {"a": 2, "b": float("inf")}, [], None,
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                model = ScriptedModel(
                    response(ToolCall("c1", "add", arguments)), ModelResponse(text="参数错误")
                )
                Harness(model, (self.add,)).run("调用")
                self.assertEqual(model.requests[1].messages[-1].tool_result.error_code, "invalid_arguments")
        self.assertEqual(self.executed, [])

    def test_tool_exception_is_safe_and_model_can_continue(self):
        def broken(a, b):
            raise RuntimeError("private-error-detail")

        tool = Tool("add", "加法", ("a", "b"), broken)
        model = ScriptedModel(response(self.call), ModelResponse(text="工具失败"))
        result = Harness(model, (tool,)).run("调用")
        self.assertEqual(model.requests[1].messages[-1].tool_result.error_code, "tool_error")
        self.assertEqual(result.reason, "completed")
        self.assertNotIn("private-error-detail", repr(result))

    def test_invalid_tool_output_is_rejected(self):
        for value in (True, "5", float("inf")):
            with self.subTest(value=value):
                tool = Tool("add", "加法", ("a", "b"), lambda a, b: value)
                model = ScriptedModel(response(self.call), ModelResponse(text="失败"))
                Harness(model, (tool,)).run("调用")
                self.assertEqual(model.requests[1].messages[-1].tool_result.error_code, "invalid_tool_result")

    def test_model_limit_stops_next_request(self):
        model = ScriptedModel(response(self.call))
        result = Harness(model, (self.add,)).run("调用", limits=RunLimits(1, 4))
        self.assertEqual((result.reason, result.model_calls, result.tool_calls), ("model_limit", 1, 1))
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.executed, [(2, 3)])

    def test_insufficient_batch_budget_executes_nothing(self):
        second = ToolCall("c2", "add", {"a": 4, "b": 5})
        model = ScriptedModel(response(self.call, second))
        result = Harness(model, (self.add,)).run("调用", limits=RunLimits(4, 1))
        self.assertEqual((result.reason, result.tool_calls), ("tool_limit", 0))
        self.assertEqual(self.executed, [])
        self.assertEqual(len(model.requests), 1)

    def test_complete_batch_preserves_order(self):
        second = ToolCall("c2", "add", {"a": 4, "b": 5})
        model = ScriptedModel(response(self.call, second), ModelResponse(text="完成"))
        Harness(model, (self.add,)).run("调用", limits=RunLimits(4, 2))
        results = [m.tool_result for m in model.requests[1].messages if m.role == "tool"]
        self.assertEqual([(r.call_id, r.value) for r in results], [("c1", 5), ("c2", 9)])
        self.assertEqual(self.executed, [(2, 3), (4, 5)])

    def test_rejected_calls_consume_budget(self):
        model = ScriptedModel(
            response(ToolCall("c1", "missing", {})),
            response(ToolCall("c2", "add", {"a": 2, "b": 3})),
        )
        result = Harness(model, (self.add,)).run("调用", limits=RunLimits(4, 1))
        self.assertEqual((result.reason, result.tool_calls), ("tool_limit", 1))
        self.assertEqual(self.executed, [])

    def test_reused_call_id_stops_before_second_execution(self):
        model = ScriptedModel(response(self.call), response(self.call))
        result = Harness(model, (self.add,)).run("调用")
        self.assertEqual((result.reason, result.tool_calls), ("protocol_error", 1))
        self.assertEqual(self.executed, [(2, 3)])

    def test_zero_tool_budget_allows_chat_but_rejects_calls(self):
        for reply, reason in ((ModelResponse(text="你好"), "completed"), (response(self.call), "tool_limit")):
            with self.subTest(reason=reason):
                result = Harness(ScriptedModel(reply), (self.add,)).run("你好", limits=RunLimits(2, 0))
                self.assertEqual((result.reason, result.tool_calls), (reason, 0))
        self.assertEqual(self.executed, [])

    def test_protocol_failure_rejects_entire_batch(self):
        replies = [None, ModelResponse(text=42), ModelResponse(tool_calls=[]), response("bad"),
                   response(ToolCall("", "add", {})), response(ToolCall("c1", "", {})),
                   response(self.call, self.call)]
        for reply in replies:
            with self.subTest(reply=reply):
                result = Harness(ScriptedModel(reply), (self.add,)).run("调用")
                self.assertEqual((result.reason, result.tool_calls), ("protocol_error", 0))
        self.assertEqual(self.executed, [])

    def test_model_exception_is_counted_and_safe(self):
        def broken(request):
            raise RuntimeError("private-error-detail")

        result = Harness(ScriptedModel(broken), (self.add,)).run("调用")
        self.assertEqual((result.reason, result.model_calls, result.tool_calls), ("model_error", 1, 0))
        self.assertNotIn("private-error-detail", repr(result))

    def test_duplicate_tool_registration_fails(self):
        with self.assertRaises(ValueError):
            Harness(ScriptedModel(), (self.add, self.add))

    def test_invalid_limits_are_rejected(self):
        for kwargs in ({"max_model_calls": 0}, {"max_model_calls": True},
                       {"max_model_calls": 1.5}, {"max_tool_calls": -1}, {"max_tool_calls": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunLimits(**kwargs)

    def test_runs_start_with_independent_history_and_counts(self):
        model = ScriptedModel(ModelResponse(text="一"), ModelResponse(text="二"))
        harness = Harness(model)
        first, second = harness.run("第一次"), harness.run("第二次")
        self.assertEqual((first.model_calls, second.model_calls), (1, 1))
        self.assertEqual(len(model.requests[1].messages), 1)
        self.assertEqual(model.requests[1].messages[0].text, "第二次")

    def test_empty_input_does_not_request_model(self):
        model = ScriptedModel()
        for prompt in ("", "  ", None):
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                Harness(model).run(prompt)
        self.assertEqual(model.requests, [])


if __name__ == "__main__":
    unittest.main()
