"""Optional vector adapter over a verified lexical corpus, not an agent loop.

The sidecar stores numbers and chunk identities, never source text. A lexical
change invalidates it immediately; an explicit rebuild replaces or clears it.
No request is sent while a SQLite transaction is held, and no fallback silently
turns a failed semantic request into lexical results.
"""
import asyncio
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import threading
import uuid

from .embeddings import EmbeddingConfig, EmbeddingError
from .knowledge import KnowledgePlugin, _index_path, MAX_HITS, MAX_QUERY_CHARS, MAX_RESULT_CHARS
from ..tools import Tool, ToolExecutionUnknown


MAX_SEMANTIC_CHUNKS = 128
BATCH_SIZE = 8
MAX_DIMENSIONS = 4096
MAX_SIDECAR_PAGES = 4096
SIDECAR_APPLICATION_ID = 0x41535456
SIDECAR_VERSION = 1
TEST_TEXT = "Astra embedding connection test."
_BASE_KEYS = {"schema", "knowledge_identity"}
_BUILT_KEYS = _BASE_KEYS | {"identity", "corpus_sha256", "dimension", "vector_count", "generation", "actual_model"}


def _error(code, **extra):
    return {"ok": False, "error_code": code, "mode": "semantic", **extra}


def _normalized(vector, dimension=None):
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= MAX_DIMENSIONS:
        raise ValueError("embedding_invalid_vector")
    if dimension is not None and len(vector) != dimension:
        raise ValueError("embedding_dimension_mismatch")
    if any(type(value) not in (int, float) for value in vector):
        raise ValueError("embedding_invalid_vector")
    try:
        values = tuple(float(value) for value in vector)
    except (ValueError, OverflowError):
        raise ValueError("embedding_invalid_vector") from None
    if any(not math.isfinite(value) for value in values):
        raise ValueError("embedding_invalid_vector")
    scale = max(abs(value) for value in values)
    if scale == 0:
        raise ValueError("embedding_zero_vector")
    # Scaling before summing also accepts valid very large/small finite vectors.
    scaled = tuple(value / scale for value in values)
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    return tuple(value / norm for value in scaled)


def _usage_add(total, usage):
    if not isinstance(usage, dict):
        return total
    result = dict(total or {})
    for key in ("prompt_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result[key] = result.get(key, 0) + value
    return result or None


class SemanticKnowledgePlugin:
    """A minimal provider seam: ``await provider.embed(texts)`` plus status.

    Custom providers are trusted host code. The declared configuration identity
    and implementation revision are bindings, not proofs of remote behavior.
    """
    def __init__(self, knowledge, provider, config, index_path=None):
        if not isinstance(knowledge, KnowledgePlugin) or not isinstance(config, EmbeddingConfig):
            raise TypeError("invalid_semantic_adapter")
        if not callable(getattr(provider, "embed", None)) or not callable(getattr(provider, "status", None)):
            raise TypeError("invalid_embedding_provider")
        if hasattr(provider, "config") and provider.config != config:
            raise ValueError("embedding_provider_config_mismatch")
        self.knowledge, self.provider, self.config = knowledge, provider, config
        self.namespace = knowledge.namespace
        self.name = self.namespace + ".semantic"
        self.permissions = ("files.read", self.namespace + ".read", self.namespace + ".write", self.namespace + ".external")
        self._lock, self._operation = threading.RLock(), threading.Lock()
        self._closed, self._db, self._last_error = False, None, None
        self.index_path = Path(str(knowledge.index_path) + ".vectors.sqlite3") if index_path is None else Path(index_path)
        payload = {"knowledge": knowledge.recovery_identity, "provider": config.identity(),
                   "adapter": "semantic-v1", "chunks": MAX_SEMANTIC_CHUNKS, "batch": BATCH_SIZE,
                   "dimensions": MAX_DIMENSIONS, "schema": SIDECAR_VERSION}
        self.index_identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        binding = getattr(provider, "recovery_identity", None)
        credential = binding() if callable(binding) else config.identity()
        if not isinstance(credential, str) or re.fullmatch(r"[0-9a-f]{64}", credential) is None:
            raise ValueError("invalid_embedding_provider_identity")
        # Credential rotation invalidates approval snapshots without invalidating
        # otherwise compatible stored vectors. Custom providers own this binding.
        self.recovery_identity = hashlib.sha256(json.dumps(
            {"index": self.index_identity, "credential": credential}, sort_keys=True).encode()).hexdigest()
        self.tools = ()
        if not config.enabled:
            return
        self.index_path = _index_path(knowledge.root, self.index_path)
        # The host additionally checks all collections and the runtime DB before
        # activation; this protects direct library callers from obvious aliases.
        forbidden = {Path(str(knowledge.index_path) + suffix).resolve()
                     for suffix in ("", "-wal", "-shm", "-journal", ".lock")}
        if self.index_path in forbidden or (self.index_path.exists() and self.index_path.samefile(knowledge.index_path)):
            raise ValueError("embedding_index_conflict")
        try:
            self._db = sqlite3.connect(self.index_path, timeout=1, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._initialize()
            self._db.execute(f"PRAGMA max_page_count={MAX_SIDECAR_PAGES}")
            self.tools = self._make_tools()
        except sqlite3.DatabaseError:
            self.close()
            raise ValueError("embedding_index_incompatible") from None
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        database = self._db
        objects = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        app_id = database.execute("PRAGMA application_id").fetchone()[0]
        if not objects and app_id == 0:
            try:
                database.execute("BEGIN IMMEDIATE")
                database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                database.execute("CREATE TABLE vectors (chunk_id TEXT PRIMARY KEY,vector BLOB NOT NULL,sha256 TEXT NOT NULL)")
                database.executemany("INSERT INTO metadata VALUES (?,?)",
                    (("schema", str(SIDECAR_VERSION)), ("knowledge_identity", self.knowledge.recovery_identity)))
                database.execute(f"PRAGMA application_id={SIDECAR_APPLICATION_ID}")
                database.execute(f"PRAGMA user_version={SIDECAR_VERSION}")
                database.commit()
            except BaseException:
                database.rollback()
                raise
        elif (objects != {"metadata", "vectors"} or app_id != SIDECAR_APPLICATION_ID
              or database.execute("PRAGMA user_version").fetchone()[0] != SIDECAR_VERSION):
            raise ValueError("embedding_index_incompatible")
        self._read_index()

    def _read_index(self):
        """Validate stored numbers before any query is sent to the provider."""
        database = self._db
        shapes = database.execute("SELECT length(key),length(value),typeof(key),typeof(value) FROM metadata LIMIT 9").fetchall()
        if len(shapes) > 8 or any(row[2:] != ("text", "text") or not 1 <= row[0] <= 32
                                  or not 0 <= row[1] <= 256 for row in shapes):
            raise ValueError("embedding_index_inconsistent")
        metadata = dict(database.execute("SELECT key,value FROM metadata"))
        if (set(metadata) not in (_BASE_KEYS, _BUILT_KEYS) or metadata.get("schema") != str(SIDECAR_VERSION)
                or metadata.get("knowledge_identity") != self.knowledge.recovery_identity):
            raise ValueError("embedding_index_inconsistent")
        sizes = database.execute("SELECT length(chunk_id),length(vector),length(sha256),typeof(chunk_id),typeof(vector),typeof(sha256) FROM vectors LIMIT ?",
                                 (MAX_SEMANTIC_CHUNKS + 1,)).fetchall()
        if len(sizes) > MAX_SEMANTIC_CHUNKS:
            raise ValueError("embedding_index_inconsistent")
        if set(metadata) == _BASE_KEYS:
            if sizes:
                raise ValueError("embedding_index_inconsistent")
            return metadata, {}
        if (any(re.fullmatch(r"[0-9a-f]{64}", metadata[key]) is None for key in ("identity", "corpus_sha256"))
                or re.fullmatch(r"[0-9a-f]{32}", metadata["generation"]) is None
                or not re.fullmatch(r"[0-9]{1,4}", metadata["dimension"])
                or not re.fullmatch(r"[0-9]{1,3}", metadata["vector_count"])
                or not metadata["actual_model"] or len(metadata["actual_model"]) > 200):
            raise ValueError("embedding_index_inconsistent")
        dimension, count = int(metadata["dimension"]), int(metadata["vector_count"])
        if (count != len(sizes) or not 0 <= count <= MAX_SEMANTIC_CHUNKS
                or (count and not 1 <= dimension <= MAX_DIMENSIONS) or (not count and dimension != 0)):
            raise ValueError("embedding_index_inconsistent")
        if any(row[3:] != ("text", "blob", "text") or row[0] != 64
               or row[1] != dimension * 8 or row[2] != 64 for row in sizes):
            raise ValueError("embedding_index_inconsistent")
        rows = database.execute("SELECT * FROM vectors ORDER BY chunk_id LIMIT ?", (MAX_SEMANTIC_CHUNKS,)).fetchall()
        vectors = {}
        for row in rows:
            raw = row["vector"]
            if (not isinstance(raw, bytes) or len(raw) != dimension * 8
                    or re.fullmatch(r"[0-9a-f]{64}", row["chunk_id"]) is None
                    or hashlib.sha256(raw).hexdigest() != row["sha256"]):
                raise ValueError("embedding_index_inconsistent")
            vector = struct.unpack("<" + "d" * dimension, raw)
            if any(not math.isfinite(value) for value in vector) or not math.isclose(math.fsum(x*x for x in vector), 1, abs_tol=1e-9):
                raise ValueError("embedding_index_inconsistent")
            vectors[row["chunk_id"]] = vector
        return metadata, vectors

    def _index_snapshot(self):
        with self._lock:
            if self._closed or self._db is None:
                raise ValueError("embedding_index_closed")
            try:
                self._db.execute("BEGIN")
                return self._read_index()
            finally:
                self._db.rollback()

    def _provider_status(self):
        value = self.provider.status()
        if not isinstance(value, dict) or value.get("state") not in {"ready", "not_configured", "missing_key", "invalid_key"}:
            return {"state": "invalid_key", "key_present": False}
        return {"state": value["state"], "key_present": value.get("key_present") is True}

    def _matches(self, metadata, vectors, snapshot):
        return (metadata.get("identity") == self.index_identity
                and metadata.get("corpus_sha256") == snapshot["corpus_sha256"]
                and metadata.get("actual_model") == self.config.model
                and (not vectors or self.config.dimensions is None or int(metadata["dimension"]) == self.config.dimensions)
                and set(vectors) == {chunk["chunk_id"] for chunk in snapshot["chunks"]})

    def status(self):
        provider = self._provider_status()
        result = {"ok": True, "state": "not_configured", "provider_status": provider["state"],
                  "key_present": provider["key_present"], "corpus_sha256": "", "document_count": 0,
                  "chunk_count": 0, "max_chunks": MAX_SEMANTIC_CHUNKS, "batch_size": BATCH_SIZE,
                  "estimated_requests": 0, "dimensions": None, "index_compatible": False,
                  "recovery_identity": self.recovery_identity}
        if not self.config.enabled:
            return result
        snapshot = self.knowledge.export_snapshot(MAX_SEMANTIC_CHUNKS)
        if not snapshot["ok"]:
            return {**result, "state": "rebuild_required", "error_code": snapshot["error_code"]}
        result.update({key: snapshot[key] for key in ("corpus_sha256", "document_count", "chunk_count")})
        result["estimated_requests"] = (snapshot["chunk_count"] + BATCH_SIZE - 1) // BATCH_SIZE
        try:
            metadata, vectors = self._index_snapshot()
        except (ValueError, sqlite3.DatabaseError):
            return {**result, "state": "rebuild_required", "error_code": "embedding_index_inconsistent"}
        compatible = self._matches(metadata, vectors, snapshot)
        result.update(dimensions=int(metadata["dimension"]) if metadata.get("dimension") else None,
                      index_compatible=compatible)
        if provider["state"] != "ready":
            result.update(state="missing_key" if provider["state"] == "invalid_key" else provider["state"],
                          error_code="embedding_" + provider["state"])
        elif "identity" not in metadata:
            result["state"] = "not_built"
        elif not compatible:
            result["state"] = "rebuild_required"
        elif self._last_error:
            result.update(state="provider_error", error_code=self._last_error)
        else:
            result["state"] = "ready"
        return result

    def _make_tools(self):
        digest = {"type": "string", "minLength": 64, "maxLength": 64}
        def tool(suffix, description, handler, properties, required, *, external=True, write=False):
            permissions = {"files.read", self.namespace + ".read"}
            if external:
                permissions.add(self.namespace + ".external")
            if write:
                permissions.add(self.namespace + ".write")
            return Tool(self.namespace + "." + suffix, description, (), handler,
                input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
                permissions=frozenset(permissions), requires_approval=external, side_effect=external,
                recovery_identity=self.recovery_identity, implementation_version="semantic-v1",
                implementation_dependencies=(_normalized, _usage_add, KnowledgePlugin.export_snapshot, KnowledgePlugin.verify_snapshot),
                implementation_constants={"chunks": MAX_SEMANTIC_CHUNKS, "batch": BATCH_SIZE,
                    "dimensions": MAX_DIMENSIONS, "result_chars": MAX_RESULT_CHARS, "test_text": TEST_TEXT,
                    "provider_identity": self.config.identity()})
        return (
            tool("embedding_status", "只读核对可选向量索引状态和语料摘要，不调用服务。", self.status, {}, [], external=False),
            tool("embedding_test", "审批后向宿主配置的服务发送固定短测试文本，可能计费；不发送材料。", self.embedding_test, {}, []),
            tool("embedding_rebuild", "审批后向宿主配置的服务发送已导入、核验的材料片段生成向量，可能计费；绑定语料摘要，失败保留旧库。", self.embedding_rebuild,
                 {"expected_corpus_sha256": digest}, ["expected_corpus_sha256"], write=True),
            tool("semantic_search", "审批后向宿主配置的服务发送本次查询生成向量，可能计费；必须先核对语料摘要，返回来源，不默退词法。", self.semantic_search,
                 {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                  "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HITS, "default": 3},
                  "expected_corpus_sha256": digest}, ["query", "expected_corpus_sha256"]),
        )

    def activate(self, registry):
        if self._closed:
            raise RuntimeError("semantic_index_closed")
        try:
            for tool in self.tools:
                registry.register(tool)
        except BaseException:
            self.close()
            raise
        return self.close

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._db is not None:
                self._db.close()

    def _ready(self):
        if self._closed:
            return _error("embedding_index_closed")
        if not self.config.enabled:
            return _error("embedding_not_configured")
        state = self._provider_status()["state"]
        return None if state == "ready" else _error("embedding_" + state)

    async def _embed(self, texts, dimension=None):
        try:
            batch = await self.provider.embed(texts)
        except EmbeddingError as error:
            self._last_error = error.code
            if error.request_issued:
                raise ToolExecutionUnknown("embedding_request_unknown") from None
            return _error(error.code)
        if (batch.model != self.config.model or not isinstance(batch.vectors, (tuple, list))
                or len(batch.vectors) != len(texts)):
            self._last_error = "embedding_protocol_error"
            raise ToolExecutionUnknown("embedding_request_unknown")
        vectors = []
        try:
            for vector in batch.vectors:
                normalized = _normalized(vector, dimension or self.config.dimensions)
                dimension = dimension or len(normalized)
                vectors.append(normalized)
        except ValueError:
            self._last_error = "embedding_dimension_or_vector_error"
            raise ToolExecutionUnknown("embedding_request_unknown") from None
        return {"ok": True, "vectors": vectors, "dimensions": dimension, "usage": _usage_add(None, batch.usage)}

    async def embedding_test(self):
        rejected = self._ready()
        if rejected:
            return rejected
        if not self._operation.acquire(blocking=False):
            return _error("embedding_busy")
        try:
            batch = await self._embed([TEST_TEXT])
            if not batch["ok"]:
                return batch
            self._last_error = None
            return {"ok": True, "model": self.config.model, "dimensions": batch["dimensions"],
                    "request_count": 1, "usage": batch["usage"]}
        finally:
            self._operation.release()

    async def embedding_rebuild(self, expected_corpus_sha256):
        if self._closed or not self.config.enabled:
            return _error("embedding_index_closed" if self._closed else "embedding_not_configured")
        if not self._operation.acquire(blocking=False):
            return _error("embedding_busy")
        try:
            snapshot = self.knowledge.verify_snapshot(expected_corpus_sha256, MAX_SEMANTIC_CHUNKS)
            if not snapshot["ok"]:
                return snapshot
            # An empty corpus only clears local derived numbers. Do not require
            # a credential for an action which will make no provider request.
            if snapshot["chunk_count"]:
                rejected = self._ready()
                if rejected:
                    return rejected
            try:
                original, _ = self._index_snapshot()
            except (ValueError, sqlite3.DatabaseError):
                return _error("embedding_index_inconsistent")
            vectors, dimension, calls, usage = [], None, 0, None
            for start in range(0, snapshot["chunk_count"], BATCH_SIZE):
                # Cancellation is allowed to propagate; no later batch is sent.
                batch = await self._embed([chunk["text"] for chunk in snapshot["chunks"][start:start+BATCH_SIZE]], dimension)
                if not batch["ok"]:
                    return {**batch, "request_count": calls, "usage": usage}
                calls += 1
                vectors.extend(batch["vectors"])
                dimension = batch["dimensions"]
                usage = _usage_add(usage, batch["usage"])
            current = self.knowledge.verify_snapshot(expected_corpus_sha256, MAX_SEMANTIC_CHUNKS)
            if not current["ok"]:
                return {**current, "request_count": calls, "usage": usage}
            with self._lock:
                if self._closed:
                    return _error("embedding_index_closed", request_count=calls, usage=usage)
                database = self._db
                try:
                    database.execute("BEGIN IMMEDIATE")
                    observed, _ = self._read_index()
                    if observed != original:
                        database.rollback()
                        return _error("embedding_index_changed", request_count=calls, usage=usage)
                    database.execute("DELETE FROM vectors")
                    database.execute("DELETE FROM metadata WHERE key NOT IN ('schema','knowledge_identity')")
                    metadata = {"identity": self.index_identity, "corpus_sha256": current["corpus_sha256"],
                                "dimension": str(dimension or 0), "vector_count": str(len(vectors)),
                                "generation": uuid.uuid4().hex, "actual_model": self.config.model}
                    database.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
                    for chunk, vector in zip(current["chunks"], vectors):
                        raw = struct.pack("<" + "d" * len(vector), *vector)
                        database.execute("INSERT INTO vectors VALUES (?,?,?)",
                                         (chunk["chunk_id"], raw, hashlib.sha256(raw).hexdigest()))
                    database.commit()
                except BaseException:
                    database.rollback()
                    raise
            self._last_error = None
            return {"ok": True, "mode": "semantic", "corpus_sha256": current["corpus_sha256"],
                    "chunk_count": len(vectors), "dimensions": dimension or 0, "request_count": calls, "usage": usage}
        finally:
            self._operation.release()

    async def semantic_search(self, query, expected_corpus_sha256, limit=3):
        rejected = self._ready()
        if rejected:
            return rejected
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS
                or "\0" in query or type(limit) is not int or not 1 <= limit <= MAX_HITS):
            return _error("invalid_query")
        try:
            query.encode("utf-8")
        except UnicodeError:
            return _error("invalid_query")
        if not self._operation.acquire(blocking=False):
            return _error("embedding_busy")
        try:
            snapshot = self.knowledge.verify_snapshot(expected_corpus_sha256, MAX_SEMANTIC_CHUNKS)
            if not snapshot["ok"]:
                return snapshot
            try:
                metadata, vectors = self._index_snapshot()
            except (ValueError, sqlite3.DatabaseError):
                return _error("embedding_index_inconsistent")
            if not self._matches(metadata, vectors, snapshot):
                return _error("embedding_rebuild_required")
            if not vectors:
                return {"ok": True, "query": query, "mode": "semantic", "hits": [], "stale_count": 0,
                        "truncated": False, "request_count": 0, "usage": None}
            batch = await self._embed([query], int(metadata["dimension"]))
            if not batch["ok"]:
                return batch
            current = self.knowledge.verify_snapshot(expected_corpus_sha256, MAX_SEMANTIC_CHUNKS)
            if not current["ok"]:
                return {**current, "request_count": 1, "usage": batch["usage"]}
            try:
                latest, _ = self._index_snapshot()
            except (ValueError, sqlite3.DatabaseError):
                return _error("embedding_index_inconsistent", request_count=1, usage=batch["usage"])
            if latest != metadata:
                return _error("embedding_index_changed", request_count=1, usage=batch["usage"])
            query_vector = batch["vectors"][0]
            ranked = sorted(current["chunks"], key=lambda chunk: (
                -math.fsum(a*b for a,b in zip(query_vector,vectors[chunk["chunk_id"]])), chunk["path"], chunk["start_offset"]))
            hits, used = [], 0
            for chunk in ranked:
                if len(hits) >= limit or used + len(chunk["text"]) > MAX_RESULT_CHARS:
                    break
                score = max(-1.0, min(1.0, math.fsum(a*b for a,b in zip(query_vector,vectors[chunk["chunk_id"]]))))
                hits.append({**chunk, "score": score})
                used += len(chunk["text"])
            self._last_error = None
            return {"ok": True, "query": query, "mode": "semantic", "hits": hits, "stale_count": 0,
                    "truncated": len(hits) < len(ranked), "request_count": 1, "usage": batch["usage"]}
        finally:
            self._operation.release()
