from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np

from modules.core.health import FeatureStatus, report_feature
try:  # pragma: no cover - optional dependency
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )
    try:  # pragma: no cover - compatibility for older pymilvus
        from pymilvus.exceptions import MilvusException
    except ImportError:  # pragma: no cover - defensive import
        MilvusException = Exception
    _MILVUS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency missing
    Collection = CollectionSchema = FieldSchema = DataType = None  # type: ignore[assignment]

    class _MissingMilvus:  # pragma: no cover - defensive stub
        def __getattr__(self, name: str):
            raise RuntimeError("pymilvus is required for vector store operations")

    connections = _MissingMilvus()  # type: ignore[assignment]
    utility = _MissingMilvus()  # type: ignore[assignment]
    MilvusException = Exception
    _MILVUS_AVAILABLE = False


__all__ = [
    "VectorDeleteStats",
    "MilvusVectorSpace",
]


@dataclass(slots=True)
class VectorDeleteStats:
    """Details about a vector delete request."""

    total_ms: float
    delete_ms: float
    flush_ms: Optional[float]
    deleted_count: Optional[int] = None
    remaining_count: Optional[int] = None
    compact_ms: Optional[float] = None


def _suggest_ivf_params(n_vectors: int) -> tuple[int, int]:
    """Suggest IVF parameters tailored to the current collection size."""

    nlist = int(max(256, min(4096, round(4 * math.sqrt(max(n_vectors, 1))))))
    nprobe = max(8, min(nlist, int(round(nlist * 0.03))))
    pow2 = 1 << (nprobe - 1).bit_length()
    nprobe = min(pow2, nlist)
    return nlist, nprobe


class MilvusVectorSpace:
    """Manage a Milvus collection and similarity operations for a data modality."""

    def __init__(
        self,
        *,
        collection_name: str,
        dim: int,
        embed_batch: Callable[[Sequence[Any]], np.ndarray],
        description: str,
        metric_type: str = "IP",
        host: str = "localhost",
        port: str = "19530",
        logger: Optional[logging.Logger] = None,
        ready_timeout: float = 30.0,
        health_key: Optional[str] = None,
        health_label: Optional[str] = None,
        health_category: str = "vectors",
    ) -> None:
        self.collection_name = collection_name
        self.dim = dim
        self.embed_batch = embed_batch
        self.description = description
        self.metric_type = metric_type
        self.host = host
        self.port = port
        self._log = logger or logging.getLogger(__name__)
        self._ready_timeout = ready_timeout
        self._milvus_available = _MILVUS_AVAILABLE
        self._milvus_warning_logged = False

        # Collection state
        self._collection: Optional[Collection] = None
        self._collection_error: Optional[Exception] = None
        self._collection_ready = Event()
        self._collection_init_started = Event()
        self._collection_state_lock = Lock()
        self._collection_not_ready_warned = False
        self._collection_error_logged = False

        # Vector search/index parameters
        self._nlist = 1024
        self._nprobe = 64
        self._logged_ivf_params = False

        # Failure tracking and callbacks
        self._failure_callbacks: list[tuple[Callable[[Exception], object], Optional[asyncio.AbstractEventLoop]]] = []
        self._last_notified_error_key: Optional[str] = None
        self._fallback_active = False

        # Operation warnings throttling
        self._vector_insert_warned = False
        self._vector_search_warned = False
        self._vector_delete_warned = False

        # Write operations share a lock to avoid concurrent insert/delete flushes
        self._write_lock = Lock()

        self._health_key = health_key or f"vector.{collection_name}"
        self._health_label = health_label or f"{description} ({collection_name})"
        self._health_category = health_category
        self._health_remedy = "Install and configure pymilvus to enable vector operations."

        if not self._milvus_available:
            self._collection_ready.set()
            self._publish_health(
                FeatureStatus.DISABLED,
                detail="pymilvus not available; vector store offline.",
                using_fallback=True,
            )
        else:
            self._publish_health(
                FeatureStatus.OK,
                detail="Milvus client detected; collection initialising.",
            )

    def _log_unavailable_once(self) -> None:
        if self._milvus_warning_logged:
            return
        self._log.warning(
            "[%s] Milvus dependency not available; vector operations disabled",
            self.collection_name,
        )
        self._milvus_warning_logged = True
        self._publish_health(
            FeatureStatus.DISABLED,
            detail="pymilvus missing; vector operations disabled.",
            using_fallback=True,
        )

    def _publish_health(
        self,
        status: FeatureStatus,
        detail: Optional[str] = None,
        using_fallback: bool = False,
    ) -> None:
        if not self._health_key:
            return
        report_feature(
            self._health_key,
            label=self._health_label,
            status=status,
            category=self._health_category,
            detail=detail,
            remedy=self._health_remedy,
            using_fallback=using_fallback,
            metadata={
                "collection": self.collection_name,
                "host": self.host,
                "port": self.port,
            },
        )

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _make_error_key(self, exc: Exception) -> str:
        return f"{type(exc).__name__}:{exc}"

    def _run_failure_callback(
        self,
        callback: Callable[[Exception], object],
        loop: Optional[asyncio.AbstractEventLoop],
        exc: Exception,
    ) -> None:
        try:
            result = callback(exc)
        except Exception:  # pragma: no cover - defensive logging
            self._log.exception("Milvus failure callback raised")
            return

        if asyncio.iscoroutine(result):  # pragma: no branch - small helper
            if loop and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(result, loop)
            else:
                self._log.debug(
                    "Discarded Milvus failure coroutine callback; no running event loop available",
                )

    def _notify_failure(self, exc: Exception, *, force: bool = False) -> None:
        key = self._make_error_key(exc)

        with self._collection_state_lock:
            if not force and self._last_notified_error_key == key:
                return
            self._last_notified_error_key = key
            callbacks = list(self._failure_callbacks)
            self._fallback_active = True
        self._publish_health(
            FeatureStatus.DEGRADED,
            detail=f"{type(exc).__name__}: {exc}",
            using_fallback=True,
        )

        for callback, loop in callbacks:
            self._run_failure_callback(callback, loop, exc)

    def register_failure_callback(self, callback: Callable[[Exception], object]) -> None:
        """Register a callback that runs when the Milvus collection fails."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        with self._collection_state_lock:
            self._failure_callbacks.append((callback, loop))
            current_error = self._collection_error

        if current_error:
            self._run_failure_callback(callback, loop, current_error)

    def _ensure_collection_initializer_started(self) -> None:
        if self._collection_init_started.is_set():
            return
        if not self._milvus_available:
            self._log_unavailable_once()
            return
        with self._collection_state_lock:
            if self._collection_init_started.is_set():
                return
            thread = Thread(
                target=self._initialize_collection,
                name=f"{self.collection_name}-setup",
                daemon=True,
            )
            thread.start()
            self._collection_init_started.set()

    def _initialize_collection(self) -> None:
        try:
            if not self._milvus_available:
                self._log_unavailable_once()
                return
            connections.connect("default", host=self.host, port=self.port)
            if not utility.has_collection(self.collection_name):
                self._log.info("Milvus collection '%s' missing; creating", self.collection_name)
                schema = CollectionSchema(
                    fields=[
                        FieldSchema(
                            name="id",
                            dtype=DataType.INT64,
                            is_primary=True,
                            auto_id=True,
                        ),
                        FieldSchema(
                            name="vector",
                            dtype=DataType.FLOAT_VECTOR,
                            dim=self.dim,
                        ),
                        FieldSchema(
                            name="category",
                            dtype=DataType.VARCHAR,
                            max_length=32,
                        ),
                        FieldSchema(
                            name="meta",
                            dtype=DataType.VARCHAR,
                            max_length=8192,
                        ),
                    ]
                )
                Collection(
                    name=self.collection_name,
                    schema=schema,
                    description=self.description,
                )
            coll = Collection(self.collection_name)
            n_vectors = coll.num_entities
            self._nlist, self._nprobe = _suggest_ivf_params(n_vectors)
            if not self._logged_ivf_params:
                self._log.info(
                    "[%s] Using IVF params: NLIST=%s, NPROBE=%s for N=%s",
                    self.collection_name,
                    self._nlist,
                    self._nprobe,
                    n_vectors,
                )
                self._logged_ivf_params = True
            if not coll.has_index():
                self._log.info(
                    "Index missing for collection '%s'; building IVF_FLAT with nlist=%s",
                    self.collection_name,
                    self._nlist,
                )
                coll.create_index(
                    field_name="vector",
                    index_params={
                        "index_type": "IVF_FLAT",
                        "metric_type": self.metric_type,
                        "params": {"nlist": self._nlist},
                    },
                )
            coll.load()
            with self._collection_state_lock:
                self._collection = coll
                self._collection_error = None
                self._collection_error_logged = False
                self._last_notified_error_key = None
                self._fallback_active = False
                self._vector_insert_warned = False
                self._vector_search_warned = False
                self._vector_delete_warned = False
            self._publish_health(
                FeatureStatus.OK,
                detail="Milvus collection ready.",
                using_fallback=False,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            with self._collection_state_lock:
                self._collection = None
                self._collection_error = exc
                self._collection_error_logged = False
                self._vector_insert_warned = False
                self._vector_search_warned = False
                self._vector_delete_warned = False
            self._log.exception(
                "Failed to initialize Milvus collection '%s': %s",
                self.collection_name,
                exc,
            )
            self._notify_failure(exc)
        finally:
            self._collection_ready.set()

    # ------------------------------------------------------------------
    # Collection access helpers
    # ------------------------------------------------------------------

    def _get_collection(self, timeout: Optional[float] = None) -> Optional[Collection]:
        if not self._milvus_available:
            self._log_unavailable_once()
            return None

        self._ensure_collection_initializer_started()
        wait_timeout = self._ready_timeout if timeout is None else timeout
        if not self._collection_ready.is_set():
            if self._should_block_for_collection(wait_timeout):
                ready = self._collection_ready.wait(wait_timeout)
                if not ready:
                    self._warn_collection_not_ready(wait_timeout)
                    return None
            else:
                self._warn_collection_not_ready(wait_timeout, non_blocking=True)
                return None

        with self._collection_state_lock:
            if self._collection is not None:
                return self._collection
            if self._collection_error is not None:
                if not self._collection_error_logged:
                    self._log.error(
                        "Milvus collection '%s' failed to initialize: %s",
                        self.collection_name,
                        self._collection_error,
                    )
                    self._collection_error_logged = True
            return None

    def _warn_collection_not_ready(self, wait_timeout: float, *, non_blocking: bool = False) -> None:
        if self._collection_not_ready_warned:
            return
        suffix = "; not blocking event loop" if non_blocking else ""
        self._log.warning(
            "Milvus collection '%s' is still loading after %.1fs; vector ops deferred%s",
            self.collection_name,
            wait_timeout,
            suffix,
        )
        self._collection_not_ready_warned = True

    def _should_block_for_collection(self, wait_timeout: float) -> bool:
        if wait_timeout == 0:
            return False
        if not self._milvus_available:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True

        # When called inside an active asyncio event loop, avoid blocking the loop thread.
        return not loop.is_running()

    # ------------------------------------------------------------------
    # Public status helpers
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        if not self._milvus_available:
            return False
        self._ensure_collection_initializer_started()
        if not self._collection_ready.is_set():
            return False
        with self._collection_state_lock:
            return self._collection is not None

    def is_fallback_active(self) -> bool:
        if not self._milvus_available:
            return True
        with self._collection_state_lock:
            return self._fallback_active

    def get_last_error(self) -> Optional[Exception]:
        with self._collection_state_lock:
            return self._collection_error

    def get_debug_info(self) -> dict[str, Any]:
        self._ensure_collection_initializer_started()
        info = {
            "milvus_dependency": self._milvus_available,
            "init_started": self._collection_init_started.is_set(),
            "ready_event": self._collection_ready.is_set(),
            "fallback_active": False,
            "collection_ready": False,
            "last_error": None,
            "host": self.host,
            "port": self.port,
            "collection": self.collection_name,
            "dimension": self.dim,
            "metric_type": self.metric_type,
            "entity_count": None,
            "has_index": None,
            "indexes": None,
        }
        with self._collection_state_lock:
            info["fallback_active"] = self._fallback_active
            info["collection_ready"] = self._collection is not None
            if self._collection_error is not None:
                info["last_error"] = f"{self._collection_error.__class__.__name__}: {self._collection_error}"

        coll = self._get_collection(timeout=0.0)
        if coll is not None:
            try:
                info["entity_count"] = int(coll.num_entities)  # type: ignore[arg-type]
            except Exception:  # pragma: no cover - defensive fallback
                info["entity_count"] = None
            try:
                info["has_index"] = bool(coll.has_index())
            except Exception:  # pragma: no cover - defensive fallback
                info["has_index"] = None
            try:
                indexes = getattr(coll, "indexes", None)
                if indexes:
                    details = []
                    for index in indexes:
                        details.append(
                            {
                                "type": getattr(index, "index_type", None),
                                "params": getattr(index, "params", None),
                            }
                        )
                    info["indexes"] = details
            except Exception:  # pragma: no cover - defensive fallback
                info["indexes"] = None
        return info

    # ------------------------------------------------------------------
    # Vector operations
    # ------------------------------------------------------------------

    def add_vector(self, item: Any, metadata: dict[str, Any]) -> Optional[int]:
        coll = self._get_collection()
        if coll is None:
            if not self._vector_insert_warned:
                self._log.warning(
                    "[%s] Skipping vector insert; Milvus collection is not ready",
                    self.collection_name,
                )
                self._vector_insert_warned = True
            return None

        vectors = self.embed_batch([item])
        if vectors.size == 0:
            return None

        vector = vectors[0]
        category = metadata.get("category") or "safe"
        metadata.setdefault("category", category)
        meta = json.dumps(metadata)
        with self._write_lock:
            data = [[vector.tolist()], [category], [meta]]
            insert_result = coll.insert(data)

        primary_keys = getattr(insert_result, "primary_keys", None) if insert_result is not None else None
        if primary_keys:
            pk = primary_keys[0]
            try:
                return int(pk)
            except (TypeError, ValueError):
                return pk
        return None

    async def delete_vectors(self, ids: Iterable[int]) -> Optional[VectorDeleteStats]:
        coll = self._get_collection()
        if coll is None:
            if not self._vector_delete_warned:
                self._log.warning(
                    "[%s] Unable to delete vectors; Milvus collection is not ready",
                    self.collection_name,
                )
                self._vector_delete_warned = True
            return None

        normalized_ids = [int(value) for value in ids if value is not None]
        if not normalized_ids:
            return None

        expr = "id in [" + ", ".join(str(value) for value in normalized_ids) + "]"

        def _delete_and_flush() -> VectorDeleteStats:
            start = time.perf_counter()
            delete_duration = 0.0
            flush_duration: Optional[float] = None
            delete_count: Optional[int] = None
            remaining_count: Optional[int] = None
            with self._write_lock:
                delete_started = time.perf_counter()
                result = coll.delete(expr)
                delete_duration = (time.perf_counter() - delete_started) * 1000
                if result is not None:
                    delete_count = getattr(result, "delete_count", None)
                    if delete_count is not None:
                        try:
                            delete_count = int(delete_count)
                        except (TypeError, ValueError):  # pragma: no cover - defensive cast
                            delete_count = None
                flush_started = time.perf_counter()
                try:
                    coll.flush()
                except MilvusException as exc:  # pragma: no cover - defensive logging
                    self._log.warning("[%s] Milvus flush after delete failed: %s", self.collection_name, exc)
                else:
                    flush_duration = (time.perf_counter() - flush_started) * 1000
                try:
                    remaining_count = int(coll.num_entities)  # type: ignore[arg-type]
                except Exception:  # pragma: no cover - defensive fallback
                    remaining_count = None

            total_duration = (time.perf_counter() - start) * 1000
            if delete_duration > 1000:
                self._log.warning(
                    "[%s] Milvus delete for %s ids took %.1f ms",
                    self.collection_name,
                    len(normalized_ids),
                    delete_duration,
                )
            if flush_duration and flush_duration > 1000:
                self._log.warning(
                    "[%s] Milvus flush after deleting %s ids took %.1f ms",
                    self.collection_name,
                    len(normalized_ids),
                    flush_duration,
                )
            return VectorDeleteStats(
                total_ms=total_duration,
                delete_ms=delete_duration,
                flush_ms=flush_duration,
                deleted_count=delete_count,
                remaining_count=remaining_count,
            )

        return await asyncio.to_thread(_delete_and_flush)

    async def reset_collection(
        self,
        *,
        compact: bool = True,
        compact_timeout: float = 60.0,
    ) -> Optional[VectorDeleteStats]:
        """Delete every vector in the backing collection."""

        coll = self._get_collection()
        if coll is None:
            if not self._vector_delete_warned:
                self._log.warning(
                    "[%s] Unable to reset collection; Milvus collection is not ready",
                    self.collection_name,
                )
                self._vector_delete_warned = True
            return None

        def _reset_and_flush() -> VectorDeleteStats:
            start = time.perf_counter()
            delete_duration = 0.0
            flush_duration: Optional[float] = None
            deleted_count: Optional[int] = None
            remaining_count: Optional[int] = None
            with self._write_lock:
                delete_started = time.perf_counter()
                result = coll.delete("id >= 0")
                delete_duration = (time.perf_counter() - delete_started) * 1000
                if result is not None:
                    deleted_count = getattr(result, "delete_count", None)
                    if deleted_count is not None:
                        try:
                            deleted_count = int(deleted_count)
                        except (TypeError, ValueError):  # pragma: no cover - defensive cast
                            deleted_count = None
                flush_started = time.perf_counter()
                try:
                    coll.flush()
                except MilvusException as exc:  # pragma: no cover - defensive logging
                    self._log.warning(
                        "[%s] Milvus flush after resetting collection failed: %s",
                        self.collection_name,
                        exc,
                    )
                else:
                    flush_duration = (time.perf_counter() - flush_started) * 1000
                try:
                    remaining_count = int(coll.num_entities)  # type: ignore[arg-type]
                except Exception:  # pragma: no cover - defensive fallback
                    remaining_count = None

            total_duration = (time.perf_counter() - start) * 1000
            if delete_duration > 1000:
                self._log.warning(
                    "[%s] Milvus reset delete took %.1f ms",
                    self.collection_name,
                    delete_duration,
                )
            if flush_duration and flush_duration > 1000:
                self._log.warning(
                    "[%s] Milvus reset flush took %.1f ms",
                    self.collection_name,
                    flush_duration,
                )
            return VectorDeleteStats(
                total_ms=total_duration,
                delete_ms=delete_duration,
                flush_ms=flush_duration,
                deleted_count=deleted_count,
                remaining_count=remaining_count,
            )

        stats = await asyncio.to_thread(_reset_and_flush)
        if not compact or stats is None:
            return stats

        def _run_compaction() -> Optional[float]:
            start = time.perf_counter()
            compact_fn = getattr(utility, "compact", None)
            if compact_fn is None:
                self._log.debug(
                    "[%s] Skipping compaction; pymilvus utility.compact is unavailable",
                    self.collection_name,
                )
                return None
            try:
                compaction_id = compact_fn(self.collection_name)
            except Exception as exc:  # pragma: no cover - defensive logging
                self._log.warning(
                    "[%s] Milvus compaction request failed: %s",
                    self.collection_name,
                    exc,
                )
                return None

            wait_fn = getattr(utility, "wait_for_compaction_completed", None)
            get_state_fn = getattr(utility, "get_compaction_state", None)
            if wait_fn is not None:
                try:
                    wait_fn(compaction_id, timeout=compact_timeout)
                except Exception as exc:  # pragma: no cover - defensive logging
                    self._log.warning(
                        "[%s] Waiting for compaction to finish failed: %s",
                        self.collection_name,
                        exc,
                    )
                    return None
                return (time.perf_counter() - start) * 1000

            if get_state_fn is not None:
                deadline = time.monotonic() + compact_timeout
                try:
                    while time.monotonic() < deadline:
                        state = get_state_fn(compaction_id)
                        if isinstance(state, dict):
                            status = state.get("state") or state.get("status")
                        else:
                            status = getattr(state, "state", None)
                        if status and str(status).lower() in {"completed", "success", "succeed", "finished"}:
                            return (time.perf_counter() - start) * 1000
                        time.sleep(0.5)
                except Exception as exc:  # pragma: no cover - defensive logging
                    self._log.warning(
                        "[%s] Polling compaction state failed: %s",
                        self.collection_name,
                        exc,
                    )
                    return None
                self._log.warning(
                    "[%s] Compaction did not finish within %.1fs",
                    self.collection_name,
                    compact_timeout,
                )
                return None

            self._log.debug(
                "[%s] Compaction APIs unavailable; skipping wait for completion",
                self.collection_name,
            )
            return (time.perf_counter() - start) * 1000

        compact_duration = await asyncio.to_thread(_run_compaction)
        if compact_duration is not None:
            stats.compact_ms = compact_duration
            refreshed = self._get_collection(timeout=0.0)
            if refreshed is not None:
                try:
                    stats.remaining_count = int(refreshed.num_entities)  # type: ignore[arg-type]
                except Exception:  # pragma: no cover - defensive fallback
                    pass

        return stats

    def query_similar_batch(
        self,
        items: Sequence[Any],
        *,
        threshold: float = 0.80,
        k: int = 20,
        min_votes: int = 1,
        categories: Optional[Sequence[str]] = None,
        filter_expr: Optional[str] = None,
    ) -> List[List[Dict[str, Any]]]:
        if not items:
            return []

        if not self.is_available() or self._fallback_active:
            if not self._vector_search_warned:
                reason = "fallback mode" if self._fallback_active else "collection unavailable"
                self._log.warning(
                    "[%s] Vector search skipped; %s",
                    self.collection_name,
                    reason,
                )
                self._vector_search_warned = True
            return [[] for _ in items]

        coll = self._get_collection()
        if coll is None:
            if not self._vector_search_warned:
                self._log.warning(
                    "[%s] Vector search skipped; Milvus collection is not ready",
                    self.collection_name,
                )
                self._vector_search_warned = True
            return [[] for _ in items]

        vectors = self.embed_batch(items)
        if vectors.size == 0:
            return [[] for _ in items]

        search_params = {
            "metric_type": self.metric_type,
            "params": {"nprobe": self._nprobe},
        }

        expr = filter_expr
        if expr is None and categories:
            normalized_categories = [cat for cat in dict.fromkeys(categories) if cat]
            if normalized_categories:
                if len(normalized_categories) == 1:
                    expr = f"category == {json.dumps(normalized_categories[0])}"
                else:
                    expr = f"category in {json.dumps(normalized_categories)}"

        results = coll.search(
            data=[vec.tolist() for vec in vectors],
            anns_field="vector",
            param=search_params,
            limit=k,
            output_fields=["category", "meta"],
            expr=expr,
        )

        allowed_categories: Optional[set[str]] = None
        if categories:
            allowed_categories = {cat for cat in categories if cat}

        formatted: List[List[Dict[str, Any]]] = []
        for vector_hits in results or []:
            if not vector_hits:
                formatted.append([])
                continue

            votes: dict[str, list[float]] = defaultdict(list)
            top_hit: dict[str, Dict[str, Any]] = {}

            for hit in vector_hits:
                sim = float(hit.score)
                if sim < threshold:
                    continue
                category = hit.entity.get("category")
                if allowed_categories is not None and category not in allowed_categories:
                    continue
                meta_json = hit.entity.get("meta")
                meta = json.loads(meta_json) if meta_json else {}
                meta["similarity"] = sim
                try:
                    meta["vector_id"] = int(hit.id)
                except (TypeError, ValueError):
                    meta["vector_id"] = hit.id
                if category is not None and "category" not in meta:
                    meta["category"] = category

                votes[category].append(sim)
                if category not in top_hit or sim > top_hit[category]["similarity"]:
                    top_hit[category] = meta

            valid_categories = {cat for cat, sims in votes.items() if len(sims) >= min_votes}
            batch_result = [top_hit[cat] for cat in valid_categories]
            batch_result.sort(key=lambda h: h["similarity"], reverse=True)
            formatted.append(batch_result)

        if len(formatted) < len(items):
            formatted.extend([[]] * (len(items) - len(formatted)))

        return formatted

    def query_similar(
        self,
        item: Any,
        *,
        threshold: float = 0.80,
        k: int = 20,
        min_votes: int = 1,
        categories: Optional[Sequence[str]] = None,
        filter_expr: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        result = self.query_similar_batch(
            [item],
            threshold=threshold,
            k=k,
            min_votes=min_votes,
            categories=categories,
            filter_expr=filter_expr,
        )
        return result[0] if result else []

    def list_entries(
        self,
        *,
        category: Optional[str] = None,
        expr: Optional[str] = None,
        limit: Optional[int] = None,
        output_fields: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        coll = self._get_collection()
        if coll is None:
            if not self._vector_search_warned:
                self._log.warning(
                    "[%s] Vector listing skipped; Milvus collection is not ready",
                    self.collection_name,
                )
                self._vector_search_warned = True
            return []

        query_expr = expr
        if category:
            category_expr = f"category == {json.dumps(category)}"
            if query_expr:
                query_expr = f"({query_expr}) and ({category_expr})"
            else:
                query_expr = category_expr
        if not query_expr:
            query_expr = "id >= 0"

        fields = list(output_fields or ["id", "category", "meta"])
        query_kwargs: dict[str, Any] = {
            "expr": query_expr,
            "output_fields": fields,
        }
        if limit is not None:
            query_kwargs["limit"] = int(limit)

        try:
            return coll.query(**query_kwargs)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover - defensive logging
            self._log.warning(
                "[%s] Failed to list vectors (expr=%s): %s",
                self.collection_name,
                query_expr,
                exc,
                exc_info=True,
            )
            return []
