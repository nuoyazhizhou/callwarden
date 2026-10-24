"""known_red.py
==============

known-red 隔离注册表工具（docs/testing/known_red/ 的本体执行器）。

职责（对应 README.md 的 quarantine 规则）：
1. **schema 校验**：所有条目字段齐全、类型/枚举合法、nodeid 形态正确、
   id 与 nodeid 无重复——缺任一 ⇒ exit 64（CI 阻断，防「半登记」流入）；
2. **到期检查**：expiry_date < 今天且 status=open ⇒ exit 2 并列出过期条目
   （对 kind=test 与 kind=workflow 一视同仁——workflow 条目不进 quarantine
   跑，但到期升级照常，这是日落条款的牙齿）；
3. **--deselect**：输出 kind=test 且 status=open 的全部 nodeid 的
   `--deselect <nodeid>` 串（单行，供 ci.yml 写入 $GITHUB_ENV 的
   DESELECT_ARGS 后追加到 pytest 命令行，与 -n auto 正交兼容）；
4. **--quarantine-list**：输出待跑 nodeid 列表（每行一个），供
   known-red-quarantine job 单独如实报数。

退出码：0=OK；2=到期未关；64=schema/用法错误。

用法::

    python cicd/known_red.py                    # 校验 + 到期检查（门禁形态）
    python cicd/known_red.py --deselect         # 发射 deselect 参数
    python cicd/known_red.py --quarantine-list  # 发射 quarantine nodeid 清单
    python cicd/known_red.py --registry <path>  # 指定注册表（测试用）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

EXIT_OK = 0
EXIT_EXPIRED = 2
EXIT_USAGE = 64

DEFAULT_REGISTRY = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "testing"
    / "known_red"
    / "known_red.json"
)

REQUIRED_FIELDS = (
    "id",
    "kind",
    "nodeids",
    "root_cause",
    "owner",
    "closure_criterion",
    "registered_date",
    "expiry_date",
    "status",
)
VALID_KINDS = ("test", "workflow")
VALID_STATUS = ("open", "closed")
DATE_FIELDS = ("registered_date", "expiry_date")


class KnownRedError(Exception):
    """schema/用法级错误（exit 64）。"""


def _parse_date(value: object, field: str, entry_id: str) -> date:
    if not isinstance(value, str):
        raise KnownRedError(
            f"条目 {entry_id} 字段 {field} 必须为 YYYY-MM-DD 字符串，"
            f"实际 {type(value).__name__}"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise KnownRedError(
            f"条目 {entry_id} 字段 {field} 非法日期 {value!r}（要求 YYYY-MM-DD）: {e}"
        )


def load_registry(path: Path) -> dict:
    """读取并解析注册表信封（schema_version + entries）。"""
    if not path.is_file():
        raise KnownRedError(f"注册表文件不存在：{path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise KnownRedError(f"注册表读取失败（要求 UTF-8）：{e}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KnownRedError(f"注册表不是合法 JSON：{e}")
    if not isinstance(data, dict):
        raise KnownRedError("注册表顶层必须为对象（schema_version + entries）")
    if "schema_version" not in data:
        raise KnownRedError("注册表缺字段 schema_version")
    if not isinstance(data["schema_version"], int):
        raise KnownRedError("注册表 schema_version 必须为整数")
    if "entries" not in data:
        raise KnownRedError("注册表缺字段 entries")
    if not isinstance(data["entries"], list):
        raise KnownRedError("注册表 entries 必须为数组")
    return data


def validate_entries(entries: list) -> None:
    """逐条目校验字段齐全性与形态；任一不符抛 KnownRedError（exit 64）。"""
    seen_ids: set = set()
    seen_nodeids: set = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise KnownRedError(f"entries[{i}] 必须为对象")
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            raise KnownRedError(f"entries[{i}] 缺字段：{', '.join(missing)}")

        eid = entry["id"]
        if not isinstance(eid, str) or not eid.strip():
            raise KnownRedError(f"entries[{i}] id 必须为非空字符串")
        if eid in seen_ids:
            raise KnownRedError(f"条目 id 重复：{eid}")
        seen_ids.add(eid)

        kind = entry["kind"]
        if kind not in VALID_KINDS:
            raise KnownRedError(
                f"条目 {eid} kind 必须为 {'/'.join(VALID_KINDS)} 之一，实际 {kind!r}"
            )

        status = entry["status"]
        if status not in VALID_STATUS:
            raise KnownRedError(
                f"条目 {eid} status 必须为 {'/'.join(VALID_STATUS)} 之一，实际 {status!r}"
            )

        nodeids = entry["nodeids"]
        if not isinstance(nodeids, list) or not all(
            isinstance(n, str) and n for n in nodeids
        ):
            raise KnownRedError(f"条目 {eid} nodeids 必须为非空字符串数组")
        if kind == "test" and not nodeids:
            raise KnownRedError(f"条目 {eid} kind=test 但 nodeids 为空（登记漂移？）")
        if kind == "workflow" and nodeids:
            raise KnownRedError(
                f"条目 {eid} kind=workflow 不得挂 nodeid"
                f"（workflow 级红非 pytest 用例，只做到期跟踪）"
            )
        for nid in nodeids:
            if "::" not in nid:
                raise KnownRedError(
                    f"条目 {eid} nodeid {nid!r} 非法（必须含 :: 的 pytest nodeid）"
                )
            if nid in seen_nodeids:
                raise KnownRedError(f"nodeid 跨条目重复登记：{nid}")
            seen_nodeids.add(nid)

        for f in ("root_cause", "owner", "closure_criterion"):
            if not isinstance(entry[f], str) or not entry[f].strip():
                raise KnownRedError(f"条目 {eid} 字段 {f} 必须为非空字符串")

        for f in DATE_FIELDS:
            _parse_date(entry[f], f, eid)


def open_test_entries(entries: list) -> list:
    """kind=test 且 status=open 的条目（deselect / quarantine 的取值域）。"""
    return [e for e in entries if e["kind"] == "test" and e["status"] == "open"]


def deselect_args(entries: list) -> List[str]:
    """发射 `--deselect <nodeid>` 串（与 pytest 命令行直接拼接）。"""
    args: List[str] = []
    for entry in open_test_entries(entries):
        for nid in entry["nodeids"]:
            args.extend(["--deselect", nid])
    return args


def quarantine_nodeids(entries: list) -> List[str]:
    """quarantine job 待跑 nodeid 清单（每行一个）。"""
    nodeids: List[str] = []
    for entry in open_test_entries(entries):
        nodeids.extend(entry["nodeids"])
    return nodeids


def check_expiry(entries: list, today: Optional[date] = None) -> List[str]:
    """到期未关条目（expiry_date < today 且 status=open），返回描述行。"""
    if today is None:
        today = date.today()
    expired: List[str] = []
    for entry in entries:
        if entry["status"] != "open":
            continue
        expiry = _parse_date(entry["expiry_date"], "expiry_date", entry["id"])
        if expiry < today:
            expired.append(
                f"{entry['id']}（owner={entry['owner']}）到期日 "
                f"{entry['expiry_date']} 已过（今天 {today.isoformat()}）仍未关闭"
            )
    return expired


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="known-red 注册表：校验 / 到期 / deselect 发射 / quarantine 清单"
    )
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help="注册表 JSON 路径（默认 docs/testing/known_red/known_red.json）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--deselect",
        action="store_true",
        help="输出 kind=test 且 status=open 的全部 nodeid 的 --deselect 串（单行）",
    )
    mode.add_argument(
        "--quarantine-list",
        action="store_true",
        help="输出待跑 nodeid 列表（每行一个）",
    )
    args = parser.parse_args(argv)

    try:
        data = load_registry(Path(args.registry))
        validate_entries(data["entries"])
    except KnownRedError as e:
        print(f"known-red registry error: {e}", file=sys.stderr)
        return EXIT_USAGE

    entries = data["entries"]

    if args.deselect:
        print(" ".join(deselect_args(entries)))
        return EXIT_OK

    if args.quarantine_list:
        for nid in quarantine_nodeids(entries):
            print(nid)
        return EXIT_OK

    # 默认门禁形态：schema 已过 ⇒ 到期检查。
    # workflow 条目与 test 条目一视同仁参与到期（不进 quarantine 跑，
    # 但到期升级照常——日落条款的牙齿不因 kind 而区别）。
    expired = check_expiry(entries)
    if expired:
        print(
            f"known-red 到期未关 {len(expired)} 条（不自动续期："
            f"须加速修复 / 重估根因 / 显式修订到期日并说明理由）：",
            file=sys.stderr,
        )
        for line in expired:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_EXPIRED

    open_test = open_test_entries(entries)
    n_nodeids = len(quarantine_nodeids(entries))
    print(
        f"OK: known-red 注册表 {len(entries)} 条"
        f"（open test {len(open_test)} 条 / {n_nodeids} nodeid 隔离中，"
        f"closed {sum(1 for e in entries if e['status'] == 'closed')} 条）；无到期未关"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
