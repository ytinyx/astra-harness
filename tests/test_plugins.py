import unittest

from astra_harness.plugins import PluginManager
from astra_harness.tools import Tool, ToolRegistry


class Plugin:
    def __init__(self, name, cleanup=None, fail=False):
        self.name, self.cleanup, self.fail = name, cleanup, fail

    def activate(self, registry):
        registry.register(Tool(self.name, self.name, (), lambda: 1))
        if self.fail:
            raise RuntimeError("activation failed")
        return self.cleanup


class PluginTests(unittest.TestCase):
    def test_failure_restores_existing_registration(self):
        registry = ToolRegistry()
        manager = PluginManager(registry)
        manager.activate(Plugin("one"))
        with self.assertRaises(RuntimeError):
            manager.activate(Plugin("two", fail=True))
        self.assertEqual([tool.name for tool in registry.tools()], ["one"])

    def test_cleanup_is_reverse_and_continues_after_error(self):
        closed = []
        registry = ToolRegistry()
        manager = PluginManager(registry)
        manager.activate(Plugin("one", lambda: closed.append("one")))

        def fail():
            closed.append("two")
            raise RuntimeError("private cleanup")

        manager.activate(Plugin("two", fail))
        registry.freeze()
        with self.assertRaisesRegex(RuntimeError, "plugin cleanup failed"):
            manager.close()
        self.assertEqual(closed, ["two", "one"])
        self.assertEqual(registry.tools(), ())
        manager.close()

    def test_duplicates_and_frozen_registry_rejected(self):
        registry = ToolRegistry()
        manager = PluginManager(registry)
        manager.activate(Plugin("one"))
        with self.assertRaises(ValueError):
            manager.activate(Plugin("one"))
        registry.freeze()
        with self.assertRaises(ValueError):
            manager.activate(Plugin("two"))
        manager.close()
        with self.assertRaises(RuntimeError):
            manager.activate(Plugin("three"))

    def test_failed_activation_restores_freeze_state(self):
        class Broken:
            def activate(self, registry):
                registry.freeze()
                raise RuntimeError("failed")

        registry = ToolRegistry()
        manager = PluginManager(registry)
        with self.assertRaises(RuntimeError):
            manager.activate(Broken())
        manager.activate(Plugin("later"))
        self.assertIsNotNone(registry.get("later"))


if __name__ == "__main__":
    unittest.main()
