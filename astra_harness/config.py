"""有界的运行配置与只读启动诊断，不保存模型凭据、不探测外部服务。"""

import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

from .engine import normalize_limits


MAX_CONFIG_BYTES = 65_536
DEFAULT_CONTEXT_CHARS = 48_000
MODEL_VARIABLES = ("ASTRA_API_KEY", "ASTRA_MODEL", "ASTRA_BASE_URL")


class ConfigurationError(ValueError):
    """只携带固定错误码；配置正文和本机路径不进入用户可见错误。"""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non_finite_json")


def _read_bounded(path, error_prefix):
    try:
        candidate = Path(path)
        # 不打开目录或管道，读取上限也适用于文件在 stat 后增长的情况。
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise OSError("not_regular_file")
        with candidate.open("rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise ConfigurationError(f"{error_prefix}_not_readable") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError(f"{error_prefix}_too_large")
    return raw


def load_runtime_config(path=None):
    """省略路径使用默认值；只接受 limits 与 context_chars 两个配置项。"""
    data = {}
    if path is not None:
        raw = _read_bounded(path, "config")
        try:
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                              parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ConfigurationError("config_invalid_json") from None
    if type(data) is not dict or set(data) - {"limits", "context_chars"}:
        raise ConfigurationError("config_invalid_fields")
    if "limits" in data and type(data["limits"]) is not dict:
        raise ConfigurationError("config_invalid_limits")
    try:
        limits = normalize_limits(data.get("limits"))
    except (ValueError, TypeError, OverflowError):
        raise ConfigurationError("config_invalid_limits") from None
    context_chars = data.get("context_chars", DEFAULT_CONTEXT_CHARS)
    if type(context_chars) is not int or context_chars < 1:
        raise ConfigurationError("config_invalid_context")
    return {"limits": limits, "context_chars": context_chars}


def load_model_environment(env_file=None, *, environ=None):
    """只读显式凭据文件；现有环境（包括空值）优先，不修改进程环境。"""
    values = {}
    if env_file is not None:
        raw = _read_bounded(env_file, "env_file")
        try:
            content = raw.decode("utf-8-sig")
            if any(ord(character) < 32 and character not in "\r\n\t" for character in content):
                raise ValueError("control_character")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not separator or key not in MODEL_VARIABLES or key in values:
                    raise ValueError("invalid_field")
                if value[:1] in {"'", '"'} or value[-1:] in {"'", '"'}:
                    if len(value) < 2 or value[0] != value[-1]:
                        raise ValueError("unpaired_quote")
                    value = value[1:-1]
                # 引号仅界定文字；不做变量展开、转义解析、命令执行或行内注释处理。
                values[key] = value
        except (ValueError, UnicodeError):
            raise ConfigurationError("env_file_invalid") from None
    environment = os.environ if environ is None else environ
    result = {"ASTRA_API_KEY": "", "ASTRA_MODEL": "", "ASTRA_BASE_URL": "https://api.deepseek.com"}
    result.update(values)
    for key in MODEL_VARIABLES:
        if key in environment:
            result[key] = environment[key]
    return result


def workspace_path(path):
    """检查原始根目录后再交给主机创建，避免 resolve 隐藏根链接。"""
    candidate = Path(os.path.abspath(path))
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return candidate
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ConfigurationError("workspace_link")
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigurationError("workspace_not_directory")
    return candidate


def _accessible_parent(path):
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.R_OK | os.W_OK | os.X_OK)


def knowledge_index_path(path, *, workspace, database, no_files=False):
    """静态检查可选索引位置；不打开 SQLite，也不创建文件或目录。"""
    if path is None:
        return None
    if no_files:
        raise ConfigurationError("knowledge_requires_files")
    try:
        if not os.fspath(path) or "\x00" in os.fspath(path):
            raise ConfigurationError("knowledge_index_invalid_path")
        target = Path(os.path.abspath(path))
        root = workspace_path(workspace)
        runtime_database = Path(os.path.abspath(database))
        # 同时比较配置位置和实际位置，避免别名把索引放回可写工作区。
        if target.is_relative_to(root) or target.resolve().is_relative_to(root.resolve()):
            raise ConfigurationError("knowledge_index_in_workspace")
        # SQLite 日志与主机锁文件也是数据库的一部分，两个库不能占用
        # 对方的旁文件名；检查尚未创建的名字，避免首次启动才产生冲突。
        index_files = {Path(str(target.resolve()) + suffix)
                       for suffix in ("", "-wal", "-shm", "-journal")}
        runtime_files = {Path(str(runtime_database.resolve()) + suffix)
                         for suffix in ("", ".lock", "-wal", "-shm", "-journal")}
        if (index_files & runtime_files
                or (target.exists() and runtime_database.exists() and target.samefile(runtime_database))):
            raise ConfigurationError("knowledge_index_is_database")
        for candidate in (target, *target.parents):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ConfigurationError("knowledge_index_link")
            if candidate == target:
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) > 1:
                    raise ConfigurationError("knowledge_index_not_file")
                if not os.access(candidate, os.R_OK | os.W_OK):
                    raise ConfigurationError("knowledge_index_not_accessible")
            elif not stat.S_ISDIR(info.st_mode):
                raise ConfigurationError("knowledge_index_not_accessible")
        if not _accessible_parent(target.parent):
            raise ConfigurationError("knowledge_index_not_accessible")
        return target
    except ConfigurationError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise ConfigurationError("knowledge_index_invalid_path") from None


def _valid_model_url(value):
    try:
        if not value or any(character.isspace() or ord(character) < 32 for character in value):
            return False
        parsed = urlsplit(value)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        return bool((parsed.scheme == "https" or local_http) and parsed.hostname
                    and parsed.port != 0 and not parsed.username and not parsed.password
                    and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def command_allowlist(paths=(), *, no_files=False):
    """Explicit host executable trust; validation never runs a command."""
    if not paths:
        return ()
    if no_files:
        raise ConfigurationError("commands_require_files")
    from .builtin.commands import validate_executables
    try:
        return validate_executables(paths)
    except (OSError, ValueError, TypeError):
        raise ConfigurationError("commands_invalid_allowlist") from None


def diagnose_startup(*, config_path=None, live=False, workspace=".local/workspace",
                     database=".local/harness.sqlite3", mcp_config=None, env_file=None, environ=None,
                     knowledge_index=None, knowledge_config=None, no_files=False, allow_commands=()):
    """静态检查：不创建目录/数据库，不请求模型，也不启动 MCP 子进程。"""
    checks = []

    def add(code, status, message):
        checks.append({"code": code, "status": status, "message": message})

    try:
        load_runtime_config(config_path)
        add("runtime_config", "ok", "运行预算与上下文配置有效。")
    except ConfigurationError:
        add("runtime_config", "error", "使用不超过 64 KiB 的 UTF-8 JSON；仅允许 limits 与正整数 context_chars，禁止重复键和凭据。")

    try:
        commands = command_allowlist(allow_commands, no_files=no_files)
    except ConfigurationError:
        add("command_allowlist", "error", "命令工具需要文件工具；每项 --allow-command 必须是可信本地原生可执行文件的绝对路径，不能使用 PATH 名称或批处理脚本。未启动进程。")
    else:
        add("command_allowlist", "ok", "已静态校验显式可执行文件列表；未运行命令，执行仍需逐次审批。"
            if commands else "命令工具默认关闭；如需启用，由启动者显式提供 --allow-command 绝对路径。")

    try:
        environment = load_model_environment(env_file, environ=environ)
        if env_file is not None:
            add("model_env_file", "ok", "已只读加载指定模型配置文件；现有进程环境优先，配置值不会显示。")
    except ConfigurationError:
        environment = {}
        add("model_env_file", "error", "模型配置文件需为不超过 64 KiB 的 UTF-8 KEY=value，仅允许三个 ASTRA 模型变量且不能重复；不执行或展开表达式。")
    if live:
        for variable, code in (("ASTRA_API_KEY", "model_key"), ("ASTRA_MODEL", "model_name")):
            present = bool(environment.get(variable, "").strip())
            add(code, "ok" if present else "error",
                f"{variable} 已设置；未验证供应商认证。" if present else f"请在后端环境或显式 --env-file 中设置 {variable}。")
        valid_url = _valid_model_url(environment.get("ASTRA_BASE_URL", "https://api.deepseek.com"))
        add("model_url", "ok" if valid_url else "error",
            "模型地址格式有效；未测试网络连接。" if valid_url else "ASTRA_BASE_URL 需为 HTTPS 地址（本机可用 HTTP），不能含凭据、查询参数或片段。")
    else:
        add("model_offline", "ok", "离线演示不需要模型凭据；真实模式可使用 doctor --live 检查。")

    try:
        root = workspace_path(workspace)
        if not _accessible_parent(root):
            add("workspace", "error", "工作区或最近已有父目录不可访问；请选择可读写的本地目录。")
        else:
            add("workspace", "ok", "工作区为可访问目录或可在启动时创建；本次没有创建或读取工作区文件。")
    except (OSError, ValueError, TypeError):
        add("workspace", "error", "工作区必须是普通目录，根目录不能是符号链接或 junction；请检查路径与权限。")

    try:
        target = Path(os.path.abspath(database))
        try:
            info = target.stat()
        except FileNotFoundError:
            valid_target = True
        else:
            valid_target = stat.S_ISREG(info.st_mode) and os.access(target, os.R_OK | os.W_OK)
        if not valid_target or not _accessible_parent(target.parent):
            add("database_path", "error", "数据库需为普通文件路径，父目录需可读写；请检查路径与权限。")
        else:
            add("database_path", "ok", "数据库路径与父目录通过基本访问检查；未打开数据库、创建锁文件或验证数据库内容。")
    except (OSError, ValueError, TypeError):
        add("database_path", "error", "数据库路径不可访问；请选择有效的本地文件路径。")
    add("database_lock", "not_checked", "数据库占用只能在实际启动时验证；请勿删除锁文件来绕过占用。")
    if knowledge_index is not None:
        try:
            knowledge_index_path(knowledge_index, workspace=workspace, database=database, no_files=no_files)
        except ConfigurationError:
            add("knowledge_index", "error", "知识索引需要启用文件工具；索引必须是工作区外独立于运行数据库的普通文件路径，不能经过链接，父目录需可访问。")
        else:
            add("knowledge_index", "ok", "知识索引路径通过静态检查；未打开索引、创建文件、导入文档或验证已有数据库内容。")
    if knowledge_config is not None:
        from .builtin.knowledge_collections import load_knowledge_config
        try:
            if knowledge_index is not None:
                raise ValueError("conflicting knowledge configuration")
            load_knowledge_config(knowledge_config, workspace=workspace, database=database, no_files=no_files)
        except (OSError, ValueError, TypeError):
            add("knowledge_config", "error", "知识库集合需为有界 UTF-8 JSON；材料目录必须位于工作区内且互不重叠，索引独立且位于工作区外。不能同时指定单库索引或禁用文件工具。")
        else:
            add("knowledge_config", "ok", "知识库集合通过静态路径与隔离检查；未打开索引、读取材料内容、导入文档或创建文件。")
    if mcp_config is not None:
        from .builtin.mcp_services import load_mcp_config
        try:
            load_mcp_config(mcp_config)
        except (OSError, ValueError, TypeError):
            add("mcp", "error", "MCP 配置需为有界 UTF-8 JSON；请检查命名服务、命令数组或显式回环 HTTP 端点与限制，或使用旧 command 配置。未启动或连接服务。")
        else:
            add("mcp", "ok", "MCP 配置结构与限制有效；未启动子进程、连接 HTTP 服务、发现工具或验证服务可用性。")
    return {"ok": not any(item["status"] == "error" for item in checks),
            "mode": "live" if live else "demo", "checks": checks}
