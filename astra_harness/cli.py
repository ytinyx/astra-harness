"""本地主机装配；CLI 使用启动配置，Web 可显式选择持久模型档案。"""

import argparse
from contextlib import ExitStack
from pathlib import Path
import json
import shutil

from .application import Application
from .config import (command_allowlist, diagnose_startup, knowledge_index_path, load_model_environment,
                     load_runtime_config, workspace_path)
from .plugins import PluginManager
from .tools import Tool, ToolRegistry
from .workspaces import WorkspacePolicy, check_existing_ancestors, workspace_factory


def seed_workspace(path):
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for source in (Path(__file__).parent / "builtin" / "samples").glob("*.md"):
        target = root / source.name
        if not target.exists():
            shutil.copyfile(source, target)
    return root


def build_application(args, resources, *, autostart=True, web_services=None):
    from .builtin.files import FilePlugin
    from .builtin.models import DemoModel
    from .builtin.async_models import AsyncOpenAICompatibleModel
    from .model_profiles import ModelProfiles
    from .mcp_profiles import MCPProfiles
    from .skill_catalog import SkillCatalog
    from .builtin.local_skills import LocalSkillLoader
    from .workspace_binding import WorkspaceError
    from .knowledge_library import KnowledgeLibrary, library_directory

    # 完整验证主机配置后才允许创建目录、复制样例或启动进程。
    runtime = load_runtime_config(getattr(args, "config", None))
    allowed_commands = command_allowlist(getattr(args, "allow_command", ()), no_files=args.no_files)
    mcp_configs = None
    if getattr(args, "mcp_config", None) is not None:
        from .builtin.mcp_services import load_mcp_config
        mcp_configs = load_mcp_config(args.mcp_config)
    workspace = workspace_path(check_existing_ancestors(args.workspace))
    collection_config = getattr(args, "knowledge_config", None)
    if collection_config is not None and getattr(args, "knowledge_index", None) is not None:
        raise ValueError("knowledge configuration forms are mutually exclusive")
    collections = None
    if collection_config is not None:
        from .builtin.knowledge_collections import load_knowledge_config
        collections = load_knowledge_config(collection_config, workspace=workspace,
                                            database=args.database, no_files=args.no_files)
    index_path = knowledge_index_path(getattr(args, "knowledge_index", None),
                                     workspace=workspace, database=args.database, no_files=args.no_files)
    index_paths = [item.index_path for item in collections or ()]
    if index_path is not None:
        index_paths.append(index_path)
    # Reserve vector sidecars even when their optional adapter is disabled.
    index_paths += [Path(str(path) + ".vectors.sqlite3") for path in list(index_paths)]
    history_root = Path(args.database).absolute().parent / "file-history"
    managed_knowledge = (not args.no_files and index_path is None and collections is None)
    library_root = library_directory(args.database)
    if managed_knowledge:
        # Reserve the whole host-owned directory, including future indexes.
        knowledge_index_path(library_root / "collections.json", workspace=workspace, database=args.database)
    workspace_policy = WorkspacePolicy(workspace, database=args.database, index_paths=index_paths,
        protected_directories=[history_root, library_root] if not args.no_files else (),
        config_paths=[getattr(args, name, None) for name in ("config", "mcp_config", "env_file", "knowledge_config")],
        enabled=not args.no_files)
    environment = load_model_environment(getattr(args, "env_file", None))
    model = (AsyncOpenAICompatibleModel(environment["ASTRA_API_KEY"], environment["ASTRA_MODEL"],
                                   environment["ASTRA_BASE_URL"]) if args.live else DemoModel())
    workspace.mkdir(parents=True, exist_ok=True)
    startup_spec = workspace_policy.inspect(workspace, allow_startup=True)
    if args.demo_files:
        seed_workspace(workspace)
    registry = ToolRegistry((Tool("add", "计算两个数的和", ("a", "b"), lambda a, b: a + b),))
    plugins = PluginManager(registry)
    resources.callback(plugins.close)
    permissions = []
    services = None
    file_plugin = None
    knowledge = None
    command_plugins = []

    def close_commands():
        # Selected workspaces may be assembled long after startup. Keep their
        # lifetimes in this one host scope; app.close runs first (ExitStack LIFO).
        failed = False
        for command_plugin in reversed(command_plugins):
            try:
                command_plugin.close()
            except Exception:
                failed = True
        command_plugins.clear()
        if failed:
            raise RuntimeError("command_cleanup_failed")

    resources.callback(close_commands)

    def file_extensions(plugin, spec):
        from .builtin.editing import FileEditPlugin
        tools = list(FileEditPlugin(plugin, history_root / spec["id"], max_backups=100).tools)
        if allowed_commands:
            from .builtin.commands import CommandPlugin
            command_plugin = CommandPlugin(plugin, allowed_executables=allowed_commands,
                                           timeout=5.0, max_output_bytes=16384, revision="1")
            command_plugins.append(command_plugin)
            tools.extend(command_plugin.tools)
        return tuple(tools)

    if not args.no_files:
        file_plugin = FilePlugin(workspace)
        plugins.activate(file_plugin)
        permissions.extend(["files.read", "files.write"])
        for tool in file_extensions(file_plugin, startup_spec):
            registry.register(tool)
            permissions.extend(sorted(set(tool.permissions) - set(permissions)))
    if index_path is not None or collections is not None:
        from .builtin.knowledge_collections import KnowledgeCollectionConfig, KnowledgeCollectionManager
        if index_path is not None:
            collections = (KnowledgeCollectionConfig("default", "工作区知识库", ".", workspace, index_path),)
        for collection in collections:
            collection.index_path.parent.mkdir(parents=True, exist_ok=True)
        knowledge = KnowledgeCollectionManager(collections, config_path=collection_config,
                                               workspace=workspace, database=args.database)
        plugins.activate(knowledge)
        permissions.extend(sorted(set(knowledge.permissions) - set(permissions)))
        if web_services is not None:
            web_services["knowledge"] = knowledge
    elif managed_knowledge:
        knowledge = KnowledgeLibrary(workspace, args.database)
        plugins.activate(knowledge)
        permissions.extend(sorted(set(knowledge.permissions) - set(permissions)))
        if web_services is not None:
            web_services["knowledge"] = knowledge
    if mcp_configs is not None:
        from .builtin.mcp_services import MCPServiceManager
        services = MCPServiceManager(mcp_configs)
        plugins.activate(services)
        permissions.extend(services.permissions)
    registry.freeze()
    tools = tuple(registry.get(spec.name) for spec in registry.specs())
    factory = workspace_factory(workspace_policy, startup_spec, tools, permissions, file_plugin,
                                file_extensions=file_extensions)
    dynamic_services = []

    def service_status():
        result = services.status() if services is not None else []
        for controller in dynamic_services:
            result.extend(controller.status())
        return result

    def updated_workspace_factory(updated_tools, updated_permissions):
        return workspace_factory(workspace_policy, startup_spec, updated_tools, updated_permissions,
                                 file_plugin, file_extensions=file_extensions)

    def prepare_task_context(selection, scope):
        return skill_catalog.prepare(selection, scope)

    app = Application(model, tools,
                      database=args.database, permissions=permissions,
                      mode="live" if args.live else "demo", workspace=workspace,
                      limits=runtime["limits"], max_context_chars=runtime["context_chars"], autostart=False,
                      service_status=service_status,
                      workspace_spec=startup_spec, workspace_factory=factory,
                      restore_selected=getattr(args, "command", "serve") == "serve",
                      model_catalog_factory=ModelProfiles,
                      restore_model_selection=getattr(args, "command", "serve") == "serve",
                      task_context_preparer=prepare_task_context)
    resources.callback(app.close)

    def skill_loader(scope):
        binding = app.require_workspace(scope["id"])
        if scope != {key: getattr(binding, key) for key in ("id", "path", "identity")}:
            raise WorkspaceError("workspace_changed")
        if binding.validate is not None:
            binding.validate()
        if binding.file_plugin is None:
            raise WorkspaceError("workspace_disabled")
        return LocalSkillLoader(binding.file_plugin)

    skill_catalog = SkillCatalog(app.store, loader_factory=skill_loader)
    # Saved dynamic configurations are metadata only. Explicit host activation
    # owns discovery and publishes a new immutable tool composition at idle.
    profiles = MCPProfiles(app, workspace=workspace, base_tools=tools,
                           base_permissions=tuple(permissions),
                           workspace_factory_builder=updated_workspace_factory,
                           service_status=service_status,
                           reserved_names=tuple(item.name for item in mcp_configs or ()))
    dynamic_services.append(profiles)
    if isinstance(knowledge, KnowledgeLibrary):
        knowledge.bind(app, profiles)
    # LIFO: close profile candidates/connections before Application and Store.
    resources.callback(profiles.close)
    if web_services is not None:
        web_services["workspaces"] = workspace_policy
        web_services["mcp_profiles"] = profiles
        web_services["skills"] = skill_catalog if not args.no_files else None
    if autostart:
        app.start()
    return app


def _wait_for_run(app, state):
    # 恢复沿用检查点的预算；外层等待不能先于合法的单次运行预算取消执行。
    timeout = max(300, state["limits"]["run_timeout"] + 30)
    return app.wait(state["id"], timeout=timeout)


def _skill_command(args):
    """Manage local consent without starting models, MCP or recovering runs."""
    from .builtin.files import FilePlugin
    from .builtin.local_skills import LocalSkillLoader
    from .skill_catalog import SkillCatalog, SkillCatalogError
    from .store import Store

    policy = WorkspacePolicy(args.workspace, database=args.database,
        protected_directories=[Path(args.database).absolute().parent / "file-history"])
    scope = policy.inspect(args.workspace, allow_startup=True)

    def loader(selected):
        policy.validate_spec(selected)
        return LocalSkillLoader(FilePlugin(selected["path"]))

    with ExitStack() as resources:
        store = Store(args.database)
        resources.callback(store.close)
        catalog = SkillCatalog(store, loader_factory=loader)
        listing = catalog.list(scope)
        if args.command == "skill-list":
            return listing
        item = next((item for item in listing["items"] if item["id"] == args.text), None)
        if item is None:
            raise SkillCatalogError("skill_not_found")
        if args.command == "skill-show":
            return catalog.view(item["id"], item["identity"], scope)
        return catalog.set_enabled(item["id"], args.command == "skill-enable",
                                   listing["revision"], item["identity"], scope)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Astra Harness 本地运行器", epilog=(
        "pause 在执行边界暂停；resume 沿用原调用计数与预算。等待审批也可暂停，恢复后仍需批准。"
        "doctor 只做静态检查，即使 --live 也不调用模型或启动 MCP。"))
    parser.add_argument("command", choices=["serve", "run", "approve", "pause", "resume", "cancel", "inspect", "doctor", "eval", "benchmark",
                                            "skill-list", "skill-show", "skill-enable", "skill-disable"])
    parser.add_argument("text", nargs="?", help="任务文本，或 approve/pause/resume/cancel/inspect 的运行 ID")
    parser.add_argument("--workspace", default=".local/workspace")
    parser.add_argument("--database", default=".local/harness.sqlite3")
    parser.add_argument("--demo-files", action="store_true", help="复制虚构会议样例，不覆盖已有文件")
    parser.add_argument("--no-files", action="store_true", help="关闭文件工具")
    parser.add_argument("--allow-command", action="append", default=[], metavar="ABS_EXE",
                        help="显式信任本地原生可执行文件；可重复。默认禁用命令工具，执行仍需审批，不能与 --no-files 同用")
    parser.add_argument("--live", action="store_true", help="使用后端环境中的真实模型配置")
    parser.add_argument("--env-file", help="显式读取三个 ASTRA 模型变量的 KEY=value 文件；进程环境优先，不自动加载 .env")
    parser.add_argument("--mcp-config", help="可信主机 JSON：servers 为具名 STDIO 或显式回环 HTTP 服务；兼容旧 command 配置")
    knowledge_options = parser.add_mutually_exclusive_group()
    knowledge_options.add_argument("--knowledge-index", help="启用单个工作区知识库；索引需位于工作区外，且不同于运行数据库")
    knowledge_options.add_argument("--knowledge-config", help="加载静态知识库集合 JSON；各集合使用独立材料目录和索引，不能与 --no-files 同用")
    parser.add_argument("--config", help="运行 JSON：limits 与 context_chars；参见 astra.example.json，不存放密钥")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session", help="续接已有会话")
    parser.add_argument("--skill", metavar="ID", help="仅 run：显式使用当前工作区已启用的一个本地 Skill")
    parser.add_argument("--deny", action="store_true", help="拒绝指定审批")
    parser.add_argument("--dataset", help="benchmark 使用的 JSONL 任务集")
    parser.add_argument("--baseline", help="benchmark 要比较的已有 report.json")
    parser.add_argument("--output-dir", default=".local/benchmarks", help="benchmark 隔离产物与报告目录")
    args = parser.parse_args(argv)
    if args.skill is not None and args.command != "run":
        parser.error("--skill 仅用于 run；每次任务独立选择")
    if args.command.startswith("skill-"):
        if (args.live or args.env_file or args.mcp_config or args.knowledge_index or args.knowledge_config
                or args.demo_files or args.no_files or args.allow_command or args.config or args.session):
            parser.error("Skill 管理只使用 --workspace/--database，不加载模型、MCP 或其他集成")
        if (args.command == "skill-list") != (args.text is None):
            parser.error("skill-list 不需要 ID；其余 Skill 命令需要目录 ID")
        try:
            print(json.dumps(_skill_command(args), ensure_ascii=False, indent=2))
            return 0
        except Exception as error:
            from .builtin.local_skills import SkillLoadError
            from .skill_catalog import SkillCatalogError
            from .workspace_binding import WorkspaceError
            code = error.code if isinstance(error, (SkillLoadError, SkillCatalogError, WorkspaceError)) else "skill_catalog_unavailable"
            print(json.dumps({"error": code}, ensure_ascii=False))
            return 1
    if args.command == "doctor":
        report = diagnose_startup(config_path=args.config, live=args.live, workspace=args.workspace,
                                  database=args.database, mcp_config=args.mcp_config, env_file=args.env_file,
                                  knowledge_index=args.knowledge_index, knowledge_config=args.knowledge_config, no_files=args.no_files,
                                  allow_commands=args.allow_command)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if (args.knowledge_index is not None or args.knowledge_config is not None) and args.command in {"eval", "benchmark"}:
        parser.error("eval 与 benchmark 使用各自隔离工具集，不接受 --knowledge-index 或 --knowledge-config；请使用运行命令或知识检索示例")
    if args.allow_command and args.command in {"eval", "benchmark"}:
        parser.error("eval 与 benchmark 使用隔离工具集，不接受 --allow-command；请使用运行命令或受控工具示例")
    if args.command == "benchmark":
        from .benchmark import DEFAULT_DATASET, benchmark
        from .builtin.async_models import AsyncOpenAICompatibleModel
        try:
            runtime = load_runtime_config(args.config)
            environment = load_model_environment(args.env_file)
            factory = (lambda: AsyncOpenAICompatibleModel(environment["ASTRA_API_KEY"], environment["ASTRA_MODEL"],
                                                      environment["ASTRA_BASE_URL"])) if args.live else None
            report = benchmark(args.dataset or DEFAULT_DATASET, args.output_dir,
                               model_factory=factory,
                               mode="live" if args.live else "demo", baseline=args.baseline,
                               limits=runtime["limits"], max_context_chars=runtime["context_chars"])
        except Exception as error:
            print(f"评测未完成（{type(error).__name__}），请检查任务集、基线与模型配置。")
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.command == "eval":
        if args.env_file is not None:
            parser.error("eval 不读取模型配置文件；真实模型任务集请使用 benchmark --live --env-file")
        if args.config is not None:
            parser.error("eval 使用固定离线恢复验收配置；自定义预算请使用 benchmark --config")
        if args.live:
            parser.error("eval 为离线恢复验收；真实模型任务集请使用 benchmark --live")
        from .evaluation import evaluate
        report = evaluate()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.command != "serve" and not args.text:
        parser.error("此命令需要任务文本或运行 ID")
    try:
        with ExitStack() as resources:
            # 管理命令不能先启动待管理任务，再在 CLI 退出时取消它。
            web_services = {}
            app = build_application(args, resources, autostart=args.command not in {"pause", "cancel", "inspect"},
                                    web_services=web_services)
            if args.command == "serve":
                from .web import serve
                print(f"Astra Harness: http://127.0.0.1:{args.port} ({app.mode})", flush=True)
                serve(app, port=args.port, knowledge=web_services.get("knowledge"),
                      workspaces=web_services.get("workspaces"), mcp_profiles=web_services.get("mcp_profiles"),
                      skills=web_services.get("skills"))
                return 0
            if args.command == "run":
                selection = None
                if args.skill is not None:
                    from .skill_catalog import SkillCatalogError
                    binding = app.require_workspace()
                    scope = {key: getattr(binding, key) for key in ("id", "path", "identity")}
                    catalog = web_services.get("skills")
                    if catalog is None:
                        raise SkillCatalogError("skill_catalog_unavailable")
                    item = next((item for item in catalog.list(scope)["items"] if item["id"] == args.skill), None)
                    if item is None:
                        raise SkillCatalogError("skill_not_found")
                    selection = {"id": item["id"], "expected_identity": item["identity"]}
                state = app.submit(args.text, session_id=args.session, task_context_selection=selection)
                state = _wait_for_run(app, state)
            elif args.command == "approve":
                state = app.approve(args.text, approved=not args.deny)
                state = _wait_for_run(app, state)
            elif args.command == "cancel":
                state = app.cancel(args.text)
            elif args.command == "pause":
                state = app.pause(args.text)
            elif args.command == "resume":
                state = app.resume(args.text)
                state = _wait_for_run(app, state)
            else:
                state = app.get_run(args.text)
            print(json.dumps({k: state.get(k) for k in (
                "id", "session_id", "status", "output", "error", "model_calls", "tool_calls", "approval",
                "pause_requested", "resume_status"
            )}, ensure_ascii=False, indent=2))
            return 0 if state["status"] in {"completed", "waiting_approval", "paused"} or (
                args.command == "pause" and state.get("pause_requested")) else 1
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        from .skill_catalog import SkillCatalogError
        from .builtin.local_skills import SkillLoadError
        from .task_context import TaskContextError
        if isinstance(error, (SkillCatalogError, SkillLoadError, TaskContextError)):
            print(json.dumps({"error": error.code}, ensure_ascii=False))
            return 1
        # 不打印模型供应商正文、密钥或完整底层异常。
        print(f"启动或运行失败（{type(error).__name__}）。请检查本地配置、数据库占用和环境变量。")
        return 1
