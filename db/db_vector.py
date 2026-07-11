"""
db_vector.py
============

代码知识图谱向量嵌入与语义搜索 Mixin 类。

提供基于向量嵌入的语义搜索与相似函数发现能力：
- 延迟加载嵌入模型（优先 sentence-transformers + jina-embeddings-v2-base-code，
  回退到本地 ollama API，均不可用则功能降级为空结果）
- 为函数符号生成向量并持久化到 symbol_embeddings 表
- 基于余弦相似度的语义搜索与相似函数发现

向量以 BLOB 形式存储（numpy.float32 + tobytes），读取时用 numpy.frombuffer 还原。
numpy / sentence_transformers / requests 均采用延迟导入，避免在模块级别引入重依赖。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ..i18n import t

# Rust/PyO3 扩展加载（可选，失败时回退到 numpy 向量化）
# 优先级: callwarden_core.batch_cosine_similarity > numpy 批量运算 > 纯 Python 循环
_rust_core = None
try:
    import callwarden_core as _rust_core  # type: ignore
except ImportError:
    _rust_core = None  # Rust 扩展未编译，使用 numpy 回退


def _batch_cosine(
    all_vecs: List[Tuple[str, Any]],
    query_vec: Any,
    query_norm: float,
    threshold: float = 0.0,
) -> List[Tuple[str, float]]:
    """批量计算余弦相似度（F8 向量化优化）

    优先使用 Rust/PyO3 扩展（callwarden_core.batch_cosine_similarity），
    回退到 numpy 矩阵运算（10-100x 加速于逐向量 Python 循环）。

    Args:
        all_vecs: [(symbol_hash, vec), ...] 候选向量列表
        query_vec: 查询向量（numpy array，已归一化前的原始向量）
        query_norm: 查询向量的范数（调用方已计算）
        threshold: 相似度下限过滤（find_similar_functions 用，默认 0 不过滤）

    Returns:
        [(symbol_hash, similarity), ...] 满足 threshold 的得分列表
    """
    if not all_vecs:
        return []

    import numpy

    hashes = [h for h, _ in all_vecs]

    # Rust 加速路径
    if _rust_core is not None:
        try:
            matrix = numpy.stack([v for _, v in all_vecs]).astype(numpy.float32)
            sims = _rust_core.batch_cosine_similarity(
                numpy.array(query_vec, dtype=numpy.float32),
                matrix,
            )
            norms = numpy.linalg.norm(matrix, axis=1)
            valid = norms > 0
            return [
                (hashes[i], float(sims[i]))
                for i in range(len(hashes))
                if valid[i] and float(sims[i]) >= threshold
            ]
        except Exception:
            pass  # Rust 路径异常，降级到 numpy

    # numpy 向量化路径（批量矩阵运算）
    matrix = numpy.stack([v for _, v in all_vecs])  # shape: (N, dim)
    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)  # shape: (N, 1)
    safe_norms = numpy.where(norms == 0, 1.0, norms)  # 避免除零
    normalized = matrix / safe_norms  # shape: (N, dim)

    q_normalized = query_vec / query_norm if query_norm > 0 else query_vec
    similarities = normalized @ q_normalized  # 矩阵乘法 shape: (N,)

    valid_mask = norms.flatten() > 0
    return [
        (hashes[i], float(similarities[i]))
        for i in range(len(hashes))
        if valid_mask[i] and float(similarities[i]) >= threshold
    ]


class VectorMixin:
    """向量嵌入与语义搜索功能 Mixin

    通过 self.conn 访问数据库连接，self._get_active_workspace_id() 获取当前工作区。
    嵌入模型采用延迟加载并缓存到 self._embedder_instance。
    """

    # 嵌入模型标识（写入 symbol_embeddings.model_version）
    _EMBEDDING_MODEL_VERSION = "jina-v2-base-code"
    # ollama 嵌入模型名称
    _OLLAMA_EMBED_MODEL = "nomic-embed-text"
    # ollama 服务地址
    _OLLAMA_BASE_URL = "http://localhost:11434"

    # ------------------------------------------------------------------
    # 嵌入模型加载
    # ------------------------------------------------------------------

    def _get_embedder(self) -> Optional[Tuple[str, Any]]:
        """延迟加载嵌入模型

        优先级：
        1. sentence-transformers + jinaai/jina-embeddings-v2-base-code（本地，CPU）
        2. ollama API（本地服务，nomic-embed-text）
        3. 返回 None，语义搜索不可用

        Returns:
            (后端类型, 模型对象) 元组；不可用时返回 None
        """
        if hasattr(self, "_embedder_instance"):
            return self._embedder_instance

        # 方式1: sentence-transformers（本地）
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                "jinaai/jina-embeddings-v2-base-code", device="cpu"
            )
            self._embedder_instance = ("sentence-transformers", model)
            return self._embedder_instance
        except Exception:
            pass

        # 方式2: ollama API（本地服务）
        try:
            import requests

            resp = requests.get(
                f"{self._OLLAMA_BASE_URL}/api/tags", timeout=2
            )
            if resp.status_code == 200:
                self._embedder_instance = ("ollama", None)
                return self._embedder_instance
        except Exception:
            pass

        # 方式3: 全部不可用
        self._embedder_instance = None
        return None

    def _embed_text(self, text: str) -> Optional[List[float]]:
        """调用嵌入模型将文本转为向量

        Args:
            text: 待嵌入文本

        Returns:
            向量列表；模型不可用或调用失败返回 None
        """
        embedder = self._get_embedder()
        if embedder is None:
            return None

        backend, model = embedder

        if backend == "sentence-transformers":
            try:
                # sentence-transformers 输出 numpy 数组
                vec = model.encode(text, convert_to_numpy=True)
                return vec.tolist()
            except Exception:
                return None

        if backend == "ollama":
            try:
                import requests

                resp = requests.post(
                    f"{self._OLLAMA_BASE_URL}/api/embeddings",
                    json={"model": self._OLLAMA_EMBED_MODEL, "prompt": text},
                    timeout=30,
                )
                if resp.status_code == 200:
                    return resp.json().get("embedding")
            except Exception:
                return None

        return None

    # ------------------------------------------------------------------
    # 向量编解码工具
    # ------------------------------------------------------------------

    @staticmethod
    def _vec_to_blob(embedding: List[float]) -> bytes:
        """向量转 BLOB 存储（float32 小端）"""
        import numpy

        return numpy.array(embedding, dtype=numpy.float32).tobytes()

    @staticmethod
    def _blob_to_vec(blob: bytes) -> Any:
        """BLOB 还原为 numpy 向量"""
        import numpy

        return numpy.frombuffer(blob, dtype=numpy.float32)

    # ------------------------------------------------------------------
    # 单符号 / 批量嵌入
    # ------------------------------------------------------------------

    def embed_symbol(self, symbol_hash: str) -> bool:
        """为单个函数生成向量嵌入

        Args:
            symbol_hash: 符号内容 hash（对应 symbol_contents.content_hash）

        Returns:
            成功写入返回 True，模型不可用或无内容返回 False
        """
        cur = self.conn.execute(
            "SELECT content FROM symbol_contents WHERE content_hash = ?",
            (symbol_hash,),
        )
        row = cur.fetchone()
        if not row or not row["content"]:
            return False

        embedding = self._embed_text(row["content"])
        if embedding is None:
            return False

        blob = self._vec_to_blob(embedding)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO symbol_embeddings
                (symbol_hash, embedding, model_version, dim, embedded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                symbol_hash,
                blob,
                self._EMBEDDING_MODEL_VERSION,
                len(embedding),
                time.time(),
            ),
        )
        self.conn.commit()
        return True

    def embed_all_symbols(
        self,
        batch_size: int = 32,
        force: bool = False,
        progress_callback=None,
    ) -> Dict[str, int]:
        """批量嵌入所有函数符号

        Phase 7.2：新增 progress_callback 参数，支持后台 job 进度上报。
        增量模式（force=False）只嵌入尚未有嵌入的符号，避免全量重算。

        Args:
            batch_size: 每批处理数量（用于日志/进度提示）
            force: True 时强制重新嵌入已存在嵌入的符号
            progress_callback: 可选，签名为 (progress: float, message: str) -> None

        Returns:
            统计字典：total / success / skipped / failed
        """
        ws_id = self._get_active_workspace_id()

        if progress_callback:
            progress_callback(0.05, "loading symbols")

        # 查询当前工作区内所有函数符号及其内容
        sql = """
            SELECT DISTINCT s.symbol_hash, sc.content
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ?
              AND s.kind IN ('fn', 'function', 'method')
              AND sc.content IS NOT NULL
              AND sc.content != ''
        """
        params: list = [ws_id]

        if not force:
            # Phase 7.2：增量模式 — 跳过已有嵌入的符号
            sql += " AND s.symbol_hash NOT IN (SELECT symbol_hash FROM symbol_embeddings)"

        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()

        total = len(rows)
        success = 0
        skipped = 0
        failed = 0

        # 模型不可用时直接返回，避免逐条失败
        if self._get_embedder() is None:
            if progress_callback:
                progress_callback(1.0, "embedder not available, all skipped")
            return {"total": total, "success": 0, "skipped": total, "failed": 0}

        if progress_callback:
            progress_callback(0.1, f"embedding {total} symbols")

        for idx, row in enumerate(rows):
            symbol_hash = row["symbol_hash"]
            content = row["content"]
            embedding = self._embed_text(content)
            if embedding is None:
                failed += 1
                continue

            try:
                blob = self._vec_to_blob(embedding)
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO symbol_embeddings
                        (symbol_hash, embedding, model_version, dim, embedded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        symbol_hash,
                        blob,
                        self._EMBEDDING_MODEL_VERSION,
                        len(embedding),
                        time.time(),
                    ),
                )
                success += 1
            except Exception:
                failed += 1
                continue

            # 按批次提交，避免长事务
            if (idx + 1) % batch_size == 0:
                self.conn.commit()

            # Phase 7.2：进度上报
            if progress_callback and (idx + 1) % 100 == 0:
                progress = 0.1 + 0.85 * (idx + 1) / total if total > 0 else 1.0
                progress_callback(progress, f"embedded {idx + 1}/{total}")

        self.conn.commit()

        if progress_callback:
            progress_callback(1.0, f"done: {success} success, {failed} failed")

        return {
            "total": total,
            "success": success,
            "skipped": skipped,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # 语义搜索 / 相似函数发现
    # ------------------------------------------------------------------

    def _load_all_embeddings(self) -> List[Tuple[str, Any]]:
        """加载所有嵌入向量

        Returns:
            (symbol_hash, numpy_vector) 列表
        """
        cur = self.conn.execute(
            "SELECT symbol_hash, embedding FROM symbol_embeddings"
        )
        results = []
        for row in cur:
            try:
                vec = self._blob_to_vec(row["embedding"])
                results.append((row["symbol_hash"], vec))
            except Exception:
                continue
        return results

    def semantic_search(
        self, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """语义搜索：根据自然语言查询找到最相关的函数

        Args:
            query: 查询文本
            top_k: 返回结果数量

        Returns:
            结果列表，每项包含 qualified_name / file_path / similarity / summary。
            嵌入模型不可用时返回空列表。
        """
        embedder = self._get_embedder()
        if embedder is None:
            print(t("cli.messages.db_vector_embedder_unavailable"))
            return []

        query_vec = self._embed_text(query)
        if query_vec is None:
            return []

        import numpy

        q = numpy.array(query_vec, dtype=numpy.float32)
        q_norm = numpy.linalg.norm(q)
        if q_norm == 0:
            return []

        all_vecs = self._load_all_embeddings()
        if not all_vecs:
            return []

        # 计算余弦相似度（向量化批量运算，替代逐向量 O(N) 循环）
        scored = _batch_cosine(all_vecs, q, q_norm)

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_k]

        if not top:
            return []

        # 一次性查询符号元信息与摘要
        ws_id = self._get_active_workspace_id()
        placeholders = ",".join("?" * len(top))
        sql = f"""
            SELECT s.symbol_hash, s.qualified_name, s.kind,
                   s.start_line, s.end_line, fi.rel_path,
                   (
                       SELECT ss.summary FROM symbol_summaries ss
                       WHERE ss.symbol_hash = s.symbol_hash
                         AND ss.is_current = 1
                       ORDER BY ss.version DESC LIMIT 1
                   ) as summary
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND s.symbol_hash IN ({placeholders})
            GROUP BY s.symbol_hash
        """
        cur = self.conn.execute(sql, [ws_id] + [h for h, _ in top])
        info_map: Dict[str, Dict[str, Any]] = {}
        for row in cur:
            info_map[row["symbol_hash"]] = {
                "qualified_name": row["qualified_name"],
                "file_path": row["rel_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "summary": row["summary"] or "",
            }

        results = []
        for symbol_hash, sim in top:
            info = info_map.get(symbol_hash)
            if not info:
                continue
            results.append(
                {
                    "qualified_name": info["qualified_name"],
                    "file_path": info["file_path"],
                    "start_line": info["start_line"],
                    "similarity": round(sim, 4),
                    "summary": info["summary"],
                }
            )
        return results

    def find_similar_functions(
        self, qualified_name: str, threshold: float = 0.8, top_k: int = 20
    ) -> List[Dict[str, Any]]:
        """相似函数发现：找出与指定函数语义相似的其他函数

        Args:
            qualified_name: 目标函数限定名
            threshold: 相似度阈值，仅返回高于此值的结果
            top_k: 返回结果数量上限

        Returns:
            相似函数列表，按相似度降序排列。
            目标函数不存在或无嵌入时返回空列表。
        """
        ws_id = self._get_active_workspace_id()

        # 获取目标函数的 symbol_hash
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived' AND s.qualified_name = ?
            LIMIT 1
            """,
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return []

        target_hash = row["symbol_hash"]

        cur = self.conn.execute(
            "SELECT embedding FROM symbol_embeddings WHERE symbol_hash = ?",
            (target_hash,),
        )
        emb_row = cur.fetchone()
        if not emb_row:
            return []

        import numpy

        target_vec = self._blob_to_vec(emb_row["embedding"])
        t_norm = numpy.linalg.norm(target_vec)
        if t_norm == 0:
            return []

        # 与所有其他函数向量比较（向量化批量运算，过滤自身）
        all_vecs = self._load_all_embeddings()
        filtered = [(h, v) for h, v in all_vecs if h != target_hash]
        scored = _batch_cosine(filtered, target_vec, t_norm, threshold=threshold)

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_k]

        if not top:
            return []

        # 批量查询元信息
        placeholders = ",".join("?" * len(top))
        sql = f"""
            SELECT s.symbol_hash, s.qualified_name, s.kind,
                   s.start_line, s.end_line, fi.rel_path,
                   (
                       SELECT ss.summary FROM symbol_summaries ss
                       WHERE ss.symbol_hash = s.symbol_hash
                         AND ss.is_current = 1
                       ORDER BY ss.version DESC LIMIT 1
                   ) as summary
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND s.symbol_hash IN ({placeholders})
            GROUP BY s.symbol_hash
        """
        cur = self.conn.execute(sql, [ws_id] + [h for h, _ in top])
        info_map: Dict[str, Dict[str, Any]] = {}
        for row in cur:
            info_map[row["symbol_hash"]] = {
                "qualified_name": row["qualified_name"],
                "file_path": row["rel_path"],
                "start_line": row["start_line"],
                "summary": row["summary"] or "",
            }

        results = []
        for symbol_hash, sim in top:
            info = info_map.get(symbol_hash)
            if not info:
                continue
            results.append(
                {
                    "qualified_name": info["qualified_name"],
                    "file_path": info["file_path"],
                    "start_line": info["start_line"],
                    "similarity": round(sim, 4),
                    "summary": info["summary"],
                }
            )
        return results


    # ------------------------------------------------------------------
    # RAG 管道：ask_codebase
    # ------------------------------------------------------------------

    def ask_codebase(
        self,
        question: str,
        top_k: int = 5,
        include_callers: int = 2,
        include_callees: int = 1,
        max_tokens: int = 4000,
    ) -> Dict[str, Any]:
        """RAG 管道：基于调用链增强的代码库问答上下文组装

        补齐 200 仓库对比中的 P0 RAG 缺失。与 Sourcegraph Cody / Continue.dev 的差异：
        - 它们：纯向量检索 + 代码片段拼接
        - callwarden：语义搜索 + 调用链上下文 + 摘要 + 缺陷/所有权元信息增强

        实现思路：
        1. 用 semantic_search 找到与问题最相关的 top_k 个函数
        2. 对每个函数，用 blast_radius 反向找 include_callers 个调用方（上下文）
        3. 用 calls 表正向找 include_callees 个被调用方（实现细节）
        4. 组装 RAG 上下文：函数代码 + 摘要 + 调用方/被调用方代码片段
        5. 用 max_tokens 限制总上下文长度（按字符数近似估算 token 数）

        Args:
            question: 自然语言问题
            top_k: 语义搜索返回的种子函数数量
            include_callers: 每个种子函数包含的调用方数量（上下文）
            include_callees: 每个种子函数包含的被调用方数量（实现细节）
            max_tokens: RAG 上下文最大 token 数（按字符数/4 近似估算）

        Returns:
            {
                "question": str,
                "seed_functions": [...],      # 语义搜索命中的种子函数
                "context_blocks": [...],      # 组装的 RAG 上下文块
                "rag_context": str,           # 拼接后的完整 RAG 上下文文本
                "estimated_tokens": int,      # 估算 token 数
                "truncated": bool,            # 是否因 max_tokens 截断
                "metadata": {
                    "total_functions_included": N,
                    "has_vector_index": bool,
                    "fallback_used": str,     # 向量不可用时的回退策略
                }
            }
        """
        # 1. 语义搜索种子函数
        seeds = self.semantic_search(question, top_k=top_k)
        fallback_used = ""

        # 向量索引不可用时回退：用关键词匹配 symbols 表
        if not seeds:
            fallback_used = "keyword_fallback"
            seeds = self._keyword_fallback_search(question, top_k=top_k)

        if not seeds:
            return {
                "question": question,
                "seed_functions": [],
                "context_blocks": [],
                "rag_context": "",
                "estimated_tokens": 0,
                "truncated": False,
                "metadata": {
                    "total_functions_included": 0,
                    "has_vector_index": False,
                    "fallback_used": fallback_used or "no_results",
                },
            }

        # 2. 组装上下文块
        context_blocks: List[Dict[str, Any]] = []
        included_hashes: set = set()
        total_chars = 0
        max_chars = max_tokens * 4  # 粗略估算：4 字符 ≈ 1 token
        truncated = False

        for seed in seeds:
            if total_chars > max_chars:
                truncated = True
                break

            symbol_hash = self._lookup_symbol_hash_by_qualified_name(seed["qualified_name"])
            if not symbol_hash or symbol_hash in included_hashes:
                continue
            included_hashes.add(symbol_hash)

            # 种子函数主体
            block = self._build_rag_block(symbol_hash, seed, role="seed")
            if block:
                context_blocks.append(block)
                total_chars += len(block.get("code", "")) + len(block.get("summary", ""))

            # 调用方上下文（who calls this）
            if include_callers > 0 and hasattr(self, "blast_radius"):
                try:
                    br = self.blast_radius(symbol_hash, depth=1)
                    callers = []
                    for layer in br.get("layers", []):
                        callers.extend(layer.get("symbols", []))
                    for caller in callers[:include_callers]:
                        if total_chars > max_chars:
                            truncated = True
                            break
                        caller_hash = caller.get("symbol_hash", "")
                        if caller_hash and caller_hash not in included_hashes:
                            included_hashes.add(caller_hash)
                            cblock = self._build_rag_block(
                                caller_hash,
                                {
                                    "qualified_name": caller.get("qualified_name", ""),
                                    "file_path": caller.get("file_path", ""),
                                    "start_line": 0,
                                    "similarity": 0.0,
                                    "summary": "",
                                },
                                role="caller",
                            )
                            if cblock:
                                context_blocks.append(cblock)
                                total_chars += len(cblock.get("code", ""))
                except Exception:
                    pass  # 调用链查询失败不阻塞 RAG

            # 被调用方上下文（what it calls）
            if include_callees > 0:
                try:
                    callees = self._get_callees_for_symbol(symbol_hash, limit=include_callees)
                    for callee in callees:
                        if total_chars > max_chars:
                            truncated = True
                            break
                        callee_hash = callee.get("symbol_hash", "")
                        if callee_hash and callee_hash not in included_hashes:
                            included_hashes.add(callee_hash)
                            cblock = self._build_rag_block(
                                callee_hash,
                                {
                                    "qualified_name": callee.get("qualified_name", ""),
                                    "file_path": callee.get("file_path", ""),
                                    "start_line": 0,
                                    "similarity": 0.0,
                                    "summary": "",
                                },
                                role="callee",
                            )
                            if cblock:
                                context_blocks.append(cblock)
                                total_chars += len(cblock.get("code", ""))
                except Exception:
                    pass

        # 3. 拼接 RAG 上下文文本
        rag_context = self._format_rag_context(question, context_blocks)
        estimated_tokens = len(rag_context) // 4

        return {
            "question": question,
            "seed_functions": seeds,
            "context_blocks": context_blocks,
            "rag_context": rag_context,
            "estimated_tokens": estimated_tokens,
            "truncated": truncated,
            "metadata": {
                "total_functions_included": len(included_hashes),
                "has_vector_index": len(seeds) > 0 and not fallback_used,
                "fallback_used": fallback_used,
            },
        }

    def _keyword_fallback_search(
        self, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """向量索引不可用时的关键词回退搜索

        从 symbols 表中按 qualified_name / name 模糊匹配查询关键词。

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            与 semantic_search 相同格式的结果列表
        """
        ws_id = self._get_active_workspace_id()
        # 提取查询中的关键词（简单分词）
        keywords = [w for w in query.replace(",", " ").replace(".", " ").split() if len(w) > 2]
        if not keywords:
            return []

        results: List[Dict[str, Any]] = []
        seen_hashes: set = set()

        for kw in keywords[:5]:  # 最多用 5 个关键词
            cur = self.conn.execute(
                """
                SELECT s.symbol_hash, s.qualified_name, s.start_line, s.end_line,
                       fi.rel_path,
                       (SELECT ss.summary FROM symbol_summaries ss
                        WHERE ss.symbol_hash = s.symbol_hash AND ss.is_current = 1
                        ORDER BY ss.version DESC LIMIT 1) as summary
                FROM symbols s
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND s.kind = 'fn'
                  AND (s.qualified_name LIKE ? OR s.name LIKE ?)
                LIMIT ?
                """,
                (ws_id, f"%{kw}%", f"%{kw}%", top_k),
            )
            for row in cur:
                h = row["symbol_hash"]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                results.append({
                    "qualified_name": row["qualified_name"],
                    "file_path": row["rel_path"],
                    "start_line": row["start_line"],
                    "similarity": 0.5,  # 关键词匹配的固定相似度
                    "summary": row["summary"] or "",
                })
                if len(results) >= top_k:
                    return results
        return results

    def _lookup_symbol_hash_by_qualified_name(self, qualified_name: str) -> str:
        """通过 qualified_name 查找 symbol_hash"""
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.status != 'archived' AND s.qualified_name = ?
            LIMIT 1
            """,
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        return row["symbol_hash"] if row else ""

    def _get_callees_for_symbol(
        self, symbol_hash: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """获取符号调用的下游函数（正向调用链）"""
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, fi.rel_path
            FROM calls c
            JOIN symbols s ON c.callee_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND c.caller_id = (SELECT id FROM symbols WHERE symbol_hash = ? LIMIT 1)
            LIMIT ?
            """,
            (ws_id, symbol_hash, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def _build_rag_block(
        self,
        symbol_hash: str,
        meta: Dict[str, Any],
        role: str = "seed",
    ) -> Optional[Dict[str, Any]]:
        """组装单个 RAG 上下文块

        Args:
            symbol_hash: 符号 hash
            meta: 元信息（qualified_name / file_path / similarity 等）
            role: seed（种子）/ caller（调用方）/ callee（被调用方）

        Returns:
            {
                "role": "seed/caller/callee",
                "qualified_name": str,
                "file_path": str,
                "start_line": int,
                "similarity": float,
                "summary": str,
                "code": str,          # 函数源码
                "has_comment": bool,
            }
        """
        ws_id = self._get_active_workspace_id()
        # 查询符号内容
        cur = self.conn.execute(
            """
            SELECT sc.content, sc.has_comment, s.start_line, s.end_line
            FROM symbols s
            JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.symbol_hash = ?
            LIMIT 1
            """,
            (ws_id, symbol_hash),
        )
        row = cur.fetchone()
        if not row:
            return None

        return {
            "role": role,
            "qualified_name": meta.get("qualified_name", ""),
            "file_path": meta.get("file_path", ""),
            "start_line": meta.get("start_line") or row["start_line"] or 0,
            "similarity": meta.get("similarity", 0.0),
            "summary": meta.get("summary", ""),
            "code": row["content"] or "",
            "has_comment": bool(row["has_comment"]),
        }

    def _format_rag_context(
        self, question: str, blocks: List[Dict[str, Any]]
    ) -> str:
        """将上下文块列表格式化为完整的 RAG 上下文文本

        格式：
            # 问题
            <question>

            # 相关代码上下文

            ## [种子] function_name (file.rs:10) — similarity: 0.85
            摘要: ...
            ```
            fn function_name() { ... }
            ```
        """
        lines: List[str] = []
        lines.append(t("cli.messages.db_vector_rag_question", question=question))
        lines.append(t("cli.messages.db_vector_rag_context_header"))

        role_labels = {
            "seed": t("cli.messages.db_vector_rag_role_seed"),
            "caller": t("cli.messages.db_vector_rag_role_caller"),
            "callee": t("cli.messages.db_vector_rag_role_callee"),
        }

        for block in blocks:
            role = role_labels.get(block.get("role", ""), t("cli.messages.db_vector_rag_role_default"))
            qn = block.get("qualified_name", "unknown")
            fp = block.get("file_path", "")
            sl = block.get("start_line", 0)
            sim = block.get("similarity", 0.0)
            summary = block.get("summary", "")
            code = block.get("code", "")

            lines.append(f"## [{role}] {qn} ({fp}:{sl}) — similarity: {sim:.4f}")
            if summary:
                lines.append(t("cli.messages.db_vector_rag_summary", summary=summary))
            if code:
                lines.append(f"```\n{code}\n```")
            lines.append("")

        return "\n".join(lines)
