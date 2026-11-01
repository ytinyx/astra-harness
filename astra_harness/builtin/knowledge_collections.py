"""Static, host-configured knowledge collections; no runtime collection CRUD."""
from dataclasses import dataclass, field
import os
from pathlib import Path, PureWindowsPath
import re

from .files import _reject_link
from .knowledge import KnowledgePlugin, _relative_parts, _reject_hidden
from .embeddings import EmbeddingConfig, OpenAICompatibleEmbeddingProvider, parse_embedding_config
from ..config import ConfigurationError, knowledge_index_path, workspace_path
from ..config_file import JSONConfigurationFile
from ..plugins import PluginManager


MAX_COLLECTIONS = 8
MAX_CONFIG_BYTES = 65_536
_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True)
class KnowledgeCollectionConfig:
    id: str
    label: str
    path: str
    root: Path
    index_path: Path
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    @property
    def namespace(self):
        return "knowledge" if self.id == "default" else "knowledge." + self.id


def _database_files(path):
    return {Path(str(Path(path).resolve()) + suffix) for suffix in ("", ".lock", "-wal", "-shm", "-journal")}


def _check_specs(configs):
    if not 1 <= len(configs) <= MAX_COLLECTIONS:
        raise ValueError("invalid_knowledge_collections")
    identifiers, roots, indexes, reserved = set(), [], [], set()
    for config in configs:
        if (not isinstance(config, KnowledgeCollectionConfig) or not isinstance(config.id, str)
                or _ID.fullmatch(config.id) is None or config.id in identifiers
                or not isinstance(config.label, str) or not config.label.strip()
                or len(config.label) > 64 or any(ord(char) < 32 for char in config.label)
                or not isinstance(config.path, str) or not isinstance(config.root, Path)
                or not isinstance(config.index_path, Path) or not isinstance(config.embedding, EmbeddingConfig)):
            raise ValueError("invalid_knowledge_collection")
        config.label.encode("utf-8")
        identifiers.add(config.id)
        root, index = config.root.resolve(), config.index_path.resolve()
        if any(root.is_relative_to(old) or old.is_relative_to(root) for old in roots):
            raise ValueError("overlapping_knowledge_collections")
        candidates = [index]
        if config.embedding.enabled:
            candidates.append(Path(str(config.index_path) + ".vectors.sqlite3").resolve())
        for candidate in candidates:
            names = _database_files(candidate)
            if names & reserved or any(candidate.exists() and old.exists() and candidate.samefile(old) for old in indexes):
                raise ValueError("knowledge_indexes_overlap")
            indexes.append(candidate)
            reserved.update(names)
        roots.append(root)
    # Also protects programmatic specs from putting one index inside another
    # collection; JSON loading adds the stricter main-workspace exclusion.
    if any(index.is_relative_to(root) for index in indexes for root in roots):
        raise ValueError("knowledge_index_inside_workspace")


def load_knowledge_config(path, *, workspace, database, no_files=False):
    """Read/validate all collections before opening an index or creating paths."""
    if no_files:
        raise ConfigurationError("knowledge_requires_files")

    try:
        source = Path(path)
        value, _ = JSONConfigurationFile(source).read()
        return _parse_knowledge_config(value, source, workspace=workspace, database=database)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        # JSON and filesystem exceptions can carry source snippets or host paths.
        raise ConfigurationError("invalid_knowledge_config") from None


def _parse_knowledge_config(value, source, *, workspace, database):
    """Shared static validation for startup and prospective non-secret saves."""
    try:
        if (type(value) is not dict or set(value) != {"collections"} or type(value["collections"]) is not list
                or not 1 <= len(value["collections"]) <= MAX_COLLECTIONS):
            raise ValueError("invalid_knowledge_config")
        main_root = workspace_path(workspace).resolve()
        if source.resolve() in _database_files(database):
            raise ValueError("knowledge_configuration_is_runtime_database")
        configs = []
        for item in value["collections"]:
            if (type(item) is not dict or not {"id", "label", "path", "index"}.issubset(item)
                    or set(item) - {"id", "label", "path", "index", "embedding"}):
                raise ValueError("invalid_knowledge_collection")
            parts = _relative_parts(item["path"])
            collection_root = main_root
            _reject_link(main_root)
            _reject_hidden(main_root)
            for part in parts:
                collection_root = collection_root / part
                _reject_link(collection_root)
                _reject_hidden(collection_root)
                if not collection_root.is_dir():
                    raise ValueError("knowledge_source_not_directory")
            collection_root = collection_root.resolve()
            if not collection_root.is_relative_to(main_root) or collection_root == main_root:
                raise ValueError("knowledge_source_outside_workspace")
            index = item["index"]
            if (not isinstance(index, str) or not index or len(index) > 500 or "\0" in index
                    or PureWindowsPath(index).drive or PureWindowsPath(index).root or Path(index).is_absolute()):
                raise ValueError("knowledge_index_not_relative")
            index_path = knowledge_index_path(source.resolve().parent / index,
                workspace=main_root, database=database)
            embedding = parse_embedding_config(item.get("embedding"))
            sidecar = Path(str(index_path) + ".vectors.sqlite3")
            if embedding.enabled:
                knowledge_index_path(sidecar, workspace=main_root, database=database)
            protected = _database_files(index_path)
            if embedding.enabled:
                protected.update(_database_files(sidecar))
            if source.resolve() in protected:
                raise ValueError("knowledge_index_is_configuration")
            configs.append(KnowledgeCollectionConfig(item["id"], item["label"],
                os.path.normcase("/".join(parts)).replace("\\", "/"), collection_root, index_path, embedding))
        _check_specs(configs)
        return tuple(sorted(configs, key=lambda config: config.id))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        # JSON and filesystem exceptions can carry source snippets or host paths.
        raise ConfigurationError("invalid_knowledge_config") from None


class KnowledgeCollectionManager:
    name = "knowledge-collections"

    def __init__(self, configs, *, config_path=None, workspace=None, database=None, provider_factory=None):
        self.configs = tuple(configs)
        _check_specs(self.configs)
        self._plugins = {}
        self._semantic = {}
        self._providers = {}
        self._provider_factory = provider_factory or OpenAICompatibleEmbeddingProvider
        if config_path is not None and (workspace is None or database is None):
            raise ValueError("knowledge_configuration_context_required")
        self._configuration = JSONConfigurationFile(config_path) if config_path is not None else None
        self._workspace, self._database = workspace, database
        self._manager = None
        self._closed = False
        self.permissions = tuple(sorted({permission for config in self.configs
            for permission in ("files.read", config.namespace + ".read", config.namespace + ".write",
                               *((config.namespace + ".external",) if config.embedding.enabled else ()))}))

    def activate(self, registry):
        if self._closed or self._manager is not None:
            raise RuntimeError("knowledge_collections_closed_or_active")
        manager = PluginManager(registry)
        self._manager = manager
        try:
            for config in self.configs:
                config.index_path.parent.mkdir(parents=True, exist_ok=True)
                plugin = KnowledgePlugin(config.root, config.index_path, namespace=config.namespace)
                manager.activate(plugin)
                self._plugins[config.id] = plugin
                if config.embedding.enabled:
                    from .semantic import SemanticKnowledgePlugin
                    provider = self._provider_factory(config.embedding)
                    semantic = SemanticKnowledgePlugin(plugin, provider, config.embedding)
                    manager.activate(semantic)
                    self._providers[config.id] = provider
                    self._semantic[config.id] = semantic
        except BaseException:
            self.close()
            raise
        return self.close

    def get(self, collection_id):
        return self._plugins.get(collection_id)

    def collections(self):
        result = []
        for config in self.configs:
            names = ["catalog", "inspect", "import_file", "search", "remove"]
            if config.id in self._semantic:
                names.extend(("embedding_status", "embedding_test", "embedding_rebuild", "semantic_search"))
            result.append({"id": config.id, "label": config.label, "source_scope": config.path,
                           "tools": {name: config.namespace + "." + name for name in names}})
        return result

    def _config(self, collection_id):
        for config in self.configs:
            if config.id == collection_id:
                return config
        raise KeyError("unknown_collection")

    def embedding_configuration(self, collection_id):
        """Non-secret control-plane state, never an embedding connection test."""
        active = self._config(collection_id)
        saved, revision = active, None
        if self._configuration is not None:
            value, revision = self._configuration.read()
            configs = _parse_knowledge_config(value, self._configuration.path,
                                               workspace=self._workspace, database=self._database)
            saved = next((config for config in configs if config.id == collection_id), None)
            if saved is None:
                raise RuntimeError("collection_removed_from_configuration")
        provider = self._providers.get(collection_id)
        active_key = bool(provider and provider.status().get("key_present"))
        saved_key = bool(saved.embedding.enabled and os.environ.get(saved.embedding.key_env, ""))
        return {"editable": self._configuration is not None,
                "saved": saved.embedding.public_config(), "active": active.embedding.public_config(),
                "active_key_present": active_key, "saved_key_present": saved_key,
                "config_revision": revision, "restart_required": saved != active}

    def save_embedding_configuration(self, collection_id, embedding, expected_revision):
        if self._closed or self._configuration is None:
            raise RuntimeError("knowledge_configuration_not_editable")
        self._config(collection_id)
        normalized = parse_embedding_config(embedding).public_config()

        def update(value):
            # Validate what the user is editing too, not just the prospective
            # result: malformed external edits must not become a repair target.
            _parse_knowledge_config(value, self._configuration.path,
                                    workspace=self._workspace, database=self._database)
            for item in value["collections"]:
                if item["id"] == collection_id:
                    item["embedding"] = normalized
                    return value
            raise RuntimeError("collection_removed_from_configuration")

        self._configuration.update(expected_revision, update,
            lambda value: _parse_knowledge_config(value, self._configuration.path,
                workspace=self._workspace, database=self._database))
        return self.embedding_configuration(collection_id)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._manager is not None:
            self._manager.close()
