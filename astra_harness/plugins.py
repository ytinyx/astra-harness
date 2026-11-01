"""可信插件的构建期事务：失败撤销注册，关闭时逆序尝试全部清理。"""

from .tools import ToolRegistry


class PluginManager:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self._active = []
        self._closed = False

    def activate(self, plugin) -> None:
        if self._closed:
            raise RuntimeError("plugin manager is closed")
        name = getattr(plugin, "name", type(plugin).__name__)
        if any(item[0] == name for item in self._active):
            raise ValueError("plugin already active")
        before = dict(self.registry._tools)
        was_frozen = self.registry._frozen
        try:
            cleanup = plugin.activate(self.registry)
            if cleanup is not None and not callable(cleanup):
                raise TypeError("plugin cleanup must be callable")
        except BaseException:
            self.registry._tools = before
            self.registry._frozen = was_frozen
            raise
        self._active.append((name, before, cleanup))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failed = False
        for _, before, cleanup in reversed(self._active):
            try:
                if cleanup is not None:
                    cleanup()
            except Exception:
                failed = True
            finally:
                self.registry._tools = before
        self._active.clear()
        if failed:
            raise RuntimeError("plugin cleanup failed")
