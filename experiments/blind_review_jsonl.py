"""
blind_review_jsonl —— P0 实验追加式 JSONL 采集。

本模块实现 Requirement 12.1（导出的 JSONL 评估记录）、12.6（两组都记录首轮/最终 finding、
verified true/false positive、verified misses、review 时长/token、reopen、apply 后缺陷/回滚）、
12.7（reveal 前后 verdict 变更）、12.8（无效样本原因保留）、12.18/12.20（披露/完整性事件）
与 12.23（非产品 Evidence 标记）的**采集侧**职责：

- 仅用文件 / JSONL 记录实验数据，**不**新建表、**不**改 schema（Property 24 / Req 12.1）。
- 追加式（append-only）：只追加单行 JSON，不改写、不截断既有记录（Req 1.7 追加性类比）。
- 中断恢复：容忍末行残缺，跳过损坏行并计数，保证已落盘记录不丢失（1.7 报告完整性）。
- 每条记录强制携带 ``non_product_evidence=True`` 标记（Req 12.23），并带单调 seq 与客户端时钟。

比率/置信区间/成功判定由任务 1.3 的评估器计算；本模块只提供原始事实记录（分子/分母输入）
与追加/恢复基础设施。错误码复用 blind_review_views 的本地 reason 注册表（同属 1.2 所有权）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import NON_PRODUCT_EVIDENCE
from .blind_review_views import (
    ViewErrorCode,
    make_view_reason,
    ViewDisclosureError,
    MinimalBlindView,
    _has_notes,
    _assert_no_prohibited_fields,
)


# ---------------------------------------------------------------------------
# 记录类型常量（每条 JSONL 记录的 record_type 取值）
# ---------------------------------------------------------------------------


class ExperimentRecordType:
    """P0 JSONL 记录类型目录。"""

    BLIND_VIEW = "blind_view"                    # 最小盲评视图与披露清单（Req 12.4/12.5/12.25）
    VERDICT_FIRST_PASS = "verdict_first_pass"    # 首轮 finding（Req 12.6）
    VERDICT_FINAL = "verdict_final"              # 最终 finding（Req 12.6）
    VERDICT_CHANGE = "verdict_change"            # reveal 前后 verdict 变更（Req 12.7）
    REVEAL_EVENT = "reveal_event"                # Implementer_Notes 揭示事件（Req 12.7）
    REVIEW_METRICS = "review_metrics"            # 时长/token/reopen/缺陷等原始计数（Req 12.6）
    INVALID_SAMPLE = "invalid_sample"            # 无效样本及原因（Req 12.8）
    DISCLOSURE_INCIDENT = "disclosure_incident"  # 披露事件（Req 12.18）
    INTEGRITY_INCIDENT = "integrity_incident"    # 完整性事件（Req 12.20）
    REOPEN_EVENT = "reopen_event"                # apply 后 reopen（Req 12.6）
    POST_APPLY_DEFECT = "post_apply_defect"      # apply 后缺陷/回滚（Req 12.6）


def canonical_incident_record_type(incident_type: str) -> str:
    """把 CLI 短类型和 JSONL 长类型统一为评估器使用的事件类型。"""
    aliases = {
        "disclosure": ExperimentRecordType.DISCLOSURE_INCIDENT,
        ExperimentRecordType.DISCLOSURE_INCIDENT: ExperimentRecordType.DISCLOSURE_INCIDENT,
        "integrity": ExperimentRecordType.INTEGRITY_INCIDENT,
        ExperimentRecordType.INTEGRITY_INCIDENT: ExperimentRecordType.INTEGRITY_INCIDENT,
    }
    return aliases.get(incident_type, incident_type)


# ---------------------------------------------------------------------------
# 追加式 JSONL 写入器
# ---------------------------------------------------------------------------


class ExperimentJsonlWriter:
    """追加式 JSONL 实验记录写入器（Req 12.1 / 1.7 追加性）。

    设计要点：
    - **只追加**：以 append 模式打开，绝不截断或改写既有行。
    - **耐久**：每条记录写一行 JSON + 换行，随后 flush + fsync，进程崩溃不丢已确认记录。
    - **可恢复**：每条记录自带单调 ``seq`` 与 ``client_clock_time``；读取侧容忍末行残缺。
    - **非产品 Evidence**：写入前强制 ``non_product_evidence=True``（Req 12.23），调用方无法关闭。

    线程安全：内部锁串行化 append，避免并发会话交错写坏单行（Req 14 多会话背景下的文件级保护）。
    """

    def __init__(self, path: str):
        """初始化写入器。

        Args:
            path: JSONL 文件路径；父目录不存在时自动创建。
        """
        self._path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # 续接已有文件的 seq：读取已有记录数作为下一个 seq 起点（恢复友好）。
        self._next_seq = self._compute_next_seq()

    @property
    def path(self) -> str:
        return self._path

    def _compute_next_seq(self) -> int:
        """扫描已有文件得到下一个 seq（最大 seq + 1）；文件不存在或损坏从 0 起。"""
        max_seq = -1
        try:
            if os.path.exists(self._path):
                for rec in self.recover_records(self._path):
                    seq = rec.get("seq")
                    if isinstance(seq, int) and seq > max_seq:
                        max_seq = seq
        except Exception:
            max_seq = -1
        return max_seq + 1

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """追加一条记录，返回实际写入的记录（含 seq / 时间戳 / 非产品标记）。

        写入前强制注入：``seq``（单调递增）、``client_clock_time``（缺省取当前客户端时钟）、
        ``non_product_evidence=True`` 与 ``NON_PRODUCT_EVIDENCE`` 标记。调用方传入的 seq /
        non_product_evidence 会被覆盖，以保证追加序列单调且记录恒为非产品 Evidence。

        Args:
            record: 记录字段（应包含 record_type / task_id / batch_id 等）。

        Returns:
            实际写入的记录 dict。

        Raises:
            ViewDisclosureError: 序列化失败（CONFIG 类失败不静默吞，fail-closed）。
        """
        if not isinstance(record, dict):
            raise ViewDisclosureError(make_view_reason(
                ViewErrorCode.VIEW_SOURCE_MISSING,
                task_id=str(record), field="record_not_dict"))

        with self._lock:
            stored: Dict[str, Any] = dict(record)
            stored["seq"] = self._next_seq
            stored.setdefault("client_clock_time", time.time())
            # 强制非产品 Evidence 标记（Req 12.23），不可被调用方覆盖为 False。
            stored["non_product_evidence"] = True
            stored[NON_PRODUCT_EVIDENCE] = True
            self._next_seq += 1

            try:
                line = json.dumps(stored, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError) as exc:
                # 序列化失败不静默吞：回退 seq 并 fail-closed。
                self._next_seq -= 1
                raise ViewDisclosureError(make_view_reason(
                    ViewErrorCode.VIEW_SOURCE_MISSING,
                    task_id=str(stored.get("task_id", "")),
                    field=f"json_serialize:{exc}"))

            # 追加单行 + flush + fsync，保证落盘耐久（中断恢复不丢已确认记录）。
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

        return stored

    @staticmethod
    def recover_records(path: str) -> Iterator[Dict[str, Any]]:
        """逐行恢复 JSONL 记录，容忍末行残缺与损坏行（Req 1.7 中断恢复）。

        生成器按文件顺序产出可解析的记录；无法解析的行被跳过（由 ``recover_with_stats``
        统计数量），从而进程崩溃导致的末行半截写入不会破坏整体读取。

        Args:
            path: JSONL 文件路径。

        Yields:
            每条可解析的记录 dict。
        """
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # 跳过损坏行（含末行残缺）
                if isinstance(obj, dict):
                    yield obj

    @staticmethod
    def recover_with_stats(path: str) -> Tuple[List[Dict[str, Any]], int]:
        """恢复记录并统计损坏行数量。

        Returns:
            (records, corrupted_line_count)：可解析记录列表与被跳过的损坏行数。
        """
        records: List[Dict[str, Any]] = []
        corrupted = 0
        if not os.path.exists(path):
            return records, corrupted
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    corrupted += 1
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
                else:
                    corrupted += 1
        return records, corrupted

    def read_records(self) -> List[Dict[str, Any]]:
        """读取全部可解析记录（便捷方法）。"""
        return list(self.recover_records(self._path))

    @staticmethod
    def file_sha256(path: str) -> str:
        """计算证据文件 SHA-256，供报告与归档清单绑定。"""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def evidence_summary(self) -> Dict[str, Any]:
        """返回当前 JSONL 的可复核摘要，不修改文件。"""
        records, corrupted = self.recover_with_stats(self._path)
        return {
            "path": os.path.abspath(self._path),
            "sha256": self.file_sha256(self._path) if os.path.exists(self._path) else None,
            "record_count": len(records),
            "corrupted_line_count": corrupted,
        }


def write_evidence_bundle(
    *,
    jsonl_path: str,
    artifacts_dir: str,
    batch_id: str,
    report: Dict[str, Any],
    reviewer_home: Optional[str] = None,
    reviewer_phase: str = "handoff",
) -> Dict[str, Any]:
    """在 JSONL 同一目录原子写出报告和证据清单。

    ``artifacts_dir`` 必须就是 JSONL 的父目录，防止 Reviewer 在 scratch 副本
    生成报告后把另一份 JSONL 当成权威证据。``handoff`` 阶段只允许盲视图；
    ``final`` 阶段允许已有评审记录，但仍绑定同一 Reviewer home、JSONL 和全部来源哈希。
    """
    if reviewer_phase not in {"handoff", "final"}:
        raise ValueError(f"unsupported reviewer_phase: {reviewer_phase!r}")
    jsonl_abs = os.path.abspath(jsonl_path)
    artifacts_abs = os.path.abspath(artifacts_dir)
    if not os.path.isfile(jsonl_abs):
        raise ValueError(f"evidence JSONL does not exist: {jsonl_abs!r}")
    if os.path.dirname(jsonl_abs) != artifacts_abs:
        raise ValueError(
            "evidence artifacts_dir must be the JSONL parent directory: "
            f"{artifacts_abs!r} != {os.path.dirname(jsonl_abs)!r}"
        )
    if reviewer_home:
        reviewer_experiments = os.path.abspath(os.path.join(
            reviewer_home, ".callwarden", "experiments"))
        if artifacts_abs != reviewer_experiments or os.path.dirname(jsonl_abs) != reviewer_experiments:
            raise ValueError("reviewer handoff must bind only the reviewer-home experiments directory")
        records = ExperimentJsonlWriter(jsonl_abs).read_records()
        if reviewer_phase == "handoff" and any(
                record.get("record_type") != "blind_view" for record in records):
            raise ValueError("reviewer handoff JSONL must contain blind_view records only")
    os.makedirs(artifacts_abs, exist_ok=True)
    writer = ExperimentJsonlWriter(jsonl_abs)
    evidence = writer.evidence_summary()
    report_payload = dict(report)
    report_payload["evidence"] = evidence
    report_path = os.path.join(artifacts_abs, f"report_{batch_id}.json")
    manifest_path = os.path.join(artifacts_abs, f"evidence_manifest_{batch_id}.json")

    def _atomic_json(path: str, payload: Dict[str, Any]) -> None:
        directory = os.path.dirname(path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".evidence.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    _atomic_json(report_path, report_payload)
    report_sha256 = ExperimentJsonlWriter.file_sha256(report_path)
    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "reviewer_phase": reviewer_phase,
        "non_product_evidence": True,
        "jsonl": evidence,
        "report": {
            "path": os.path.abspath(report_path),
            "sha256": report_sha256,
        },
    }
    if reviewer_home:
        blind_dir = os.path.join(artifacts_abs, f"blind_package_{batch_id}")
        config_path = os.path.join(artifacts_abs, "batch_config.json")
        sample_manifest_path = os.path.join(artifacts_abs, f"manifest_{batch_id}.json")
        for required in (blind_dir, config_path, sample_manifest_path):
            if not os.path.exists(required):
                raise ValueError(f"reviewer handoff source missing: {required!r}")
        entries = []
        for base, _, names in os.walk(blind_dir):
            for name in names:
                path = os.path.join(base, name)
                entries.append({"relative_path": os.path.relpath(path, blind_dir).replace("\\", "/"),
                                "size": os.path.getsize(path), "sha256": ExperimentJsonlWriter.file_sha256(path)})
        entries.sort(key=lambda entry: entry["relative_path"])
        blind_manifest_path = os.path.join(artifacts_abs, f"blind_package_manifest_{batch_id}.json")
        _atomic_json(blind_manifest_path, {"batch_id": batch_id, "files": entries, "non_product_evidence": True})
        review_record_count = sum(
            1 for record in records if record.get("record_type") != "blind_view")
        manifest.update({"review_started": bool(review_record_count),
                         "review_record_count": review_record_count,
                         "blind_package_manifest": {"path": os.path.abspath(blind_manifest_path), "sha256": ExperimentJsonlWriter.file_sha256(blind_manifest_path)},
                         "batch_config": {"path": os.path.abspath(config_path), "sha256": ExperimentJsonlWriter.file_sha256(config_path)},
                         "sample_manifest": {"path": os.path.abspath(sample_manifest_path), "sha256": ExperimentJsonlWriter.file_sha256(sample_manifest_path)}})
    _atomic_json(manifest_path, manifest)
    return {
        "manifest_path": os.path.abspath(manifest_path),
        "manifest_sha256": ExperimentJsonlWriter.file_sha256(manifest_path),
        "report_path": os.path.abspath(report_path),
        "report_sha256": report_sha256,
        "jsonl": evidence,
    }


# ---------------------------------------------------------------------------
# 记录构建器（原始事实；比率由 1.3 评估器计算）
# ---------------------------------------------------------------------------


def build_blind_view_record(
    view: MinimalBlindView,
    batch_id: str,
    client_clock_time: Optional[float] = None,
) -> Dict[str, Any]:
    """构造最小盲评视图记录（Req 12.4 / 12.5 / 12.25）。

    把 MinimalBlindView 的披露清单与排除清单连同分组/阶段一并落盘，标注为实验披露清单
    （非 View_Manifest）与非产品 Evidence。

    Args:
        view: 已构造的 MinimalBlindView。
        batch_id: 实验批次 ID。
        client_clock_time: 客户端时钟时间（缺省取当前）。

    Returns:
        记录 dict（待 ExperimentJsonlWriter.append 写入）。
    """
    view_dict = view.to_dict()
    return {
        "record_type": ExperimentRecordType.BLIND_VIEW,
        "task_id": view.task_id,
        "batch_id": batch_id,
        "group": view_dict["group"],
        "phase": view_dict["phase"],
        "disclosed_fields": view_dict["disclosed_fields"],
        "excluded_fields": view_dict["excluded_fields"],
        # 记录实际投影内容，而不仅是字段清单；否则 JSONL 无法重放 Reviewer
        # 在首轮 verdict 时看到的事实。Treatment 的 notes 只会在 post-reveal 视图中出现。
        "payload": copy.deepcopy(view_dict["payload"]),
        "implementer_notes_included": view_dict["implementer_notes_included"],
        "disclosure_label": view_dict["disclosure_label"],
        "is_view_manifest": False,
        "client_clock_time": client_clock_time if client_clock_time is not None else time.time(),
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }


def build_review_metrics_record(
    task_id: str,
    batch_id: str,
    group: str,
    first_pass_findings: int,
    final_findings: int,
    verified_true_positives: int,
    verified_false_positives: int,
    verified_misses: int,
    review_duration_seconds: float,
    token_usage: Optional[int],
    reopen_events: int,
    post_apply_defects: int,
    post_apply_rollbacks: int = 0,
    token_usage_source: str = "legacy_unspecified",
    token_usage_unavailable_reason: Optional[str] = None,
    observation_window_id: str = "",
    client_clock_time: Optional[float] = None,
) -> Dict[str, Any]:
    """构造 review 指标原始计数记录（Req 12.6）。

    记录两组都需采集的原始事实：首轮/最终 finding、verified true/false positive、
    verified misses（锁定 recall 分母所用）、review 时长、token 用量、reopen 事件、
    apply 后缺陷/回滚，并绑定锁定的观察窗口。比率与置信区间由 1.3 评估器计算。

    Returns:
        记录 dict。
    """
    if token_usage_source not in {"real", "unavailable", "legacy_unspecified"}:
        raise ValueError(f"unsupported token_usage_source: {token_usage_source}")
    if token_usage_source == "real":
        if token_usage is None or int(token_usage) < 0:
            raise ValueError("real token usage must be a non-negative integer")
        if token_usage_unavailable_reason:
            raise ValueError("real token usage cannot have an unavailable reason")
    elif token_usage_source == "unavailable":
        if token_usage is not None:
            raise ValueError("unavailable token usage must be recorded as null")
        if not token_usage_unavailable_reason or not token_usage_unavailable_reason.strip():
            raise ValueError("unavailable token usage requires a non-empty reason")
    elif token_usage_unavailable_reason:
        raise ValueError("legacy token usage cannot have an unavailable reason")

    return {
        "record_type": ExperimentRecordType.REVIEW_METRICS,
        "task_id": task_id,
        "batch_id": batch_id,
        "group": group,
        "first_pass_findings": int(first_pass_findings),
        "final_findings": int(final_findings),
        "verified_true_positives": int(verified_true_positives),
        "verified_false_positives": int(verified_false_positives),
        "verified_misses": int(verified_misses),
        "review_duration_seconds": float(review_duration_seconds),
        "token_usage": (int(token_usage) if token_usage is not None else None),
        "token_usage_source": token_usage_source,
        "token_usage_unavailable_reason": token_usage_unavailable_reason,
        "reopen_events": int(reopen_events),
        "post_apply_defects": int(post_apply_defects),
        "post_apply_rollbacks": int(post_apply_rollbacks),
        "observation_window_id": observation_window_id,
        "client_clock_time": client_clock_time if client_clock_time is not None else time.time(),
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }


def build_invalid_sample_record(
    task_id: str,
    batch_id: str,
    reason_code: str,
    reason_detail: str = "",
    client_clock_time: Optional[float] = None,
) -> Dict[str, Any]:
    """构造无效样本记录（Req 12.8）。

    保留 invalid 原因，供评估器把样本排除出效果估计与全部成功/暂停指标的分子分母
    （仅计入无效样本率）。reason_code 建议取 InvalidSampleReason（protocol）或
    ViewErrorCode.INVALID_SAMPLE 的语义值。

    Returns:
        记录 dict。
    """
    return {
        "record_type": ExperimentRecordType.INVALID_SAMPLE,
        "task_id": task_id,
        "batch_id": batch_id,
        "invalid_reason_code": reason_code,
        "invalid_reason_detail": reason_detail,
        "client_clock_time": client_clock_time if client_clock_time is not None else time.time(),
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }


def build_incident_record(
    task_id: str,
    batch_id: str,
    incident_type: str,
    reason_code: str,
    reason_detail: str = "",
    client_clock_time: Optional[float] = None,
) -> Dict[str, Any]:
    """构造事件记录（披露事件 Req 12.18 / 完整性事件 Req 12.20）。

    Args:
        incident_type: ExperimentRecordType.DISCLOSURE_INCIDENT 或 INTEGRITY_INCIDENT。
        reason_code: 稳定错误码（如 ViewErrorCode.DISCLOSURE_VIOLATION / INTEGRITY_INCIDENT）。
        reason_detail: 附加说明。

    Returns:
        记录 dict。
    """
    return {
        # 保持既有 CLI JSONL 短类型兼容；读取/评估侧通过
        # canonical_incident_record_type 统一短、长两种历史格式。
        "record_type": incident_type,
        "task_id": task_id,
        "batch_id": batch_id,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "client_clock_time": client_clock_time if client_clock_time is not None else time.time(),
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }


def build_reveal_event_record(
    task_id: str,
    batch_id: str,
    first_verdict_sealed: bool,
    client_clock_time: Optional[float] = None,
    verdict_changed: Optional[bool] = None,
    change_reason_code: str = "no_change",
    structured_reason: Optional[Dict[str, Any]] = None,
    implementer_notes: Optional[str] = None,
) -> Dict[str, Any]:
    """构造 reveal 事件及可选的 verdict 变更事实（Req 12.7 / 12.4）。

    Reveal 事件本身记录顺序事实；当调用方提供 ``implementer_notes`` 时，把
    Treatment post-reveal 揭示的 Implementer_Notes 作为**可审计来源**写入同一追加
    记录（Req 12.7 揭示内容留痕；12.13 证据链）。notes 必须非空且不得包含禁止披露
    字段，否则 fail-closed（复用视图层校验，禁止隐藏推理历史）。不写 notes 的旧调用方
    仍得到兼容的顺序记录。

    ``first_verdict_sealed`` 故意不在此处自动改成 True：若调用方违反顺序，记录
    必须保留 false 事实，交由评估器标记为无效/披露事件，而不能伪造通过。
    """
    if _has_notes(implementer_notes):
        # 禁止在揭示内容中携带隐藏推理/既有 verdict 等字段（Req 12.7 / 13.6）。
        _assert_no_prohibited_fields(task_id, implementer_notes)
    elif implementer_notes is not None:
        # 显式传了空/纯空白 notes：视为缺少可审计揭示来源，fail-closed。
        raise ViewDisclosureError(make_view_reason(
            ViewErrorCode.VIEW_SOURCE_MISSING,
            task_id=task_id,
            field="implementer_notes",
        ))

    record: Dict[str, Any] = {
        "record_type": ExperimentRecordType.REVEAL_EVENT,
        "task_id": task_id,
        "batch_id": batch_id,
        "first_verdict_sealed_before_reveal": bool(first_verdict_sealed),
        "client_clock_time": client_clock_time if client_clock_time is not None else time.time(),
        "non_product_evidence": True,
        NON_PRODUCT_EVIDENCE: True,
    }
    if _has_notes(implementer_notes):
        record["implementer_notes"] = implementer_notes
        record["implementer_notes_included"] = True
    if verdict_changed is not None:
        # 复用视图层的结构化原因校验，尤其是禁止嵌套 hidden reasoning。
        from .blind_review_views import build_verdict_change_record

        change = build_verdict_change_record(
            task_id=task_id,
            batch_id=batch_id,
            verdict_changed=verdict_changed,
            change_reason_code=change_reason_code,
            structured_reason=structured_reason,
            client_clock_time=record["client_clock_time"],
        )
        record.update({
            "verdict_changed": change["verdict_changed"],
            "change_reason_code": change["change_reason_code"],
            "structured_reason": change["structured_reason"],
        })
    return record
