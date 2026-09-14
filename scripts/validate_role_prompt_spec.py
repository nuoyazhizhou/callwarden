#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RP-00 validator: coverage and artifact-hash checks for the Role Prompt Compiler v1
frozen planning artifacts.

Validates (fail-closed, stdlib only):
  1. byte-level artifact hashes against the successor manifest
     (frozen spec / source clause inventory / review resolution matrix / gate manifest)
  2. inventory structural integrity (unique clause ids, per-source counts, source anchors)
  3. one-to-one clause coverage between inventory and resolution matrix,
     with dispositions restricted to the matrix allowed set
  4. manifest freeze state: RP-00-released task ids resolved, RP-01..RP-10 task ids
     still placeholders, creation authorization flags consistent with release order

Exit code 0 = pass; 1 = one or more structured errors (printed as JSON on stderr).
"""
import argparse
import hashlib
import json
import os
import sys


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(inventory_path, resolution_path, spec_path, manifest_path, repo_root=None):
    """Run all checks. Returns list of error dicts (empty == pass)."""
    errors = []
    repo_root = os.path.abspath(repo_root or os.getcwd())

    def err(code, message):
        errors.append({"code": code, "message": message})

    def repo_path(rel):
        return os.path.join(repo_root, rel.replace("/", os.sep))

    try:
        inv = _load_json(inventory_path)
        mat = _load_json(resolution_path)
        man = _load_json(manifest_path)
    except (OSError, ValueError) as exc:
        return [{"code": "E_JSON_LOAD", "message": f"无法加载输入 JSON: {exc}"}]

    # ---- 1. artifact hashes ---------------------------------------------
    inv_sha = _sha256_file(inventory_path)
    mat_sha = _sha256_file(resolution_path)
    spec_sha = _sha256_file(spec_path)

    comp = man.get("companion_artifacts", {})
    frozen = man.get("source", {}).get("frozen_spec_sha256", "")

    if frozen != spec_sha:
        err("E_SPEC_HASH", f"manifest 冻结 spec hash {frozen[:16]}.. != 实际 {spec_sha[:16]}..")
    if inv.get("frozen_spec", {}).get("sha256", "") != spec_sha:
        err("E_SPEC_HASH", "inventory.frozen_spec.sha256 与实际 spec 不一致")
    if mat.get("frozen_spec", {}).get("sha256", "") != spec_sha:
        err("E_SPEC_HASH", "matrix.frozen_spec.sha256 与实际 spec 不一致")

    if comp.get("source_clause_inventory", {}).get("sha256", "") != inv_sha:
        err("E_INVENTORY_HASH", "manifest 记录的 inventory sha256 与实际文件不一致")
    if mat.get("inventory", {}).get("sha256", "") != inv_sha:
        err("E_INVENTORY_HASH", "matrix.inventory.sha256 与实际 inventory 文件不一致")
    if comp.get("review_resolution_matrix", {}).get("sha256", "") != mat_sha:
        err("E_MATRIX_HASH", "manifest 记录的 matrix sha256 与实际文件不一致")

    gate_rel = comp.get("gate_manifest", {}).get("path", "")
    gate_sha = comp.get("gate_manifest", {}).get("sha256", "")
    if gate_rel and gate_sha:
        gate_abs = repo_path(gate_rel)
        if not os.path.isfile(gate_abs):
            err("E_GATE_MANIFEST", f"gate manifest 文件缺失: {gate_rel}")
        elif _sha256_file(gate_abs) != gate_sha:
            err("E_GATE_MANIFEST", "gate manifest sha256 与实际文件不一致")

    # ---- 2. inventory structural integrity ------------------------------
    clauses = inv.get("clauses", [])
    ids = [c.get("clause_id", "") for c in clauses]
    if inv.get("clause_count") != len(clauses):
        err("E_INVENTORY_COUNT", "inventory.clause_count 与 clauses 长度不一致")
    if len(ids) != len(set(ids)):
        err("E_INVENTORY_DUP", "inventory 存在重复 clause_id")
    if not ids:
        err("E_INVENTORY_EMPTY", "inventory 无 clauses")
    src_sum = sum(s.get("clause_count", 0) for s in inv.get("sources", []))
    if src_sum != len(clauses):
        err("E_INVENTORY_SOURCES", f"sources clause_count 总和 {src_sum} != clauses {len(clauses)}")
    for i, c in enumerate(clauses):
        missing = [k for k in ("source_path", "line_start", "section", "kind", "source_sha256")
                   if not c.get(k)]
        if missing:
            err("E_INVENTORY_ANCHOR", f"clause[{i}] {c.get('clause_id', '?')} 缺锚点字段 {missing}")
            break  # one report is enough; anchors are uniform by construction

    # ---- 3. one-to-one coverage -----------------------------------------
    res = mat.get("resolutions", [])
    res_ids = [r.get("clause_id", "") for r in res]
    if mat.get("resolution_count") != len(res):
        err("E_MATRIX_COUNT", "matrix.resolution_count 与 resolutions 长度不一致")
    if len(res_ids) != len(set(res_ids)):
        err("E_MATRIX_DUP", "matrix 存在重复 resolution")
    inv_set, res_set = set(ids), set(res_ids)
    only_inv = sorted(inv_set - res_set)
    only_mat = sorted(res_set - inv_set)
    if only_inv:
        err("E_COVERAGE_GAP", f"{len(only_inv)} 个 inventory clause 无 resolution（如 {only_inv[:3]}）")
    if only_mat:
        err("E_COVERAGE_EXTRA", f"{len(only_mat)} 个 resolution 无对应 inventory clause（如 {only_mat[:3]}）")
    allowed = set(mat.get("allowed_dispositions", []))
    bad_disp = sorted({r.get("disposition", "") for r in res} - allowed)
    if bad_disp:
        err("E_DISPOSITION", f"越界 disposition: {bad_disp}")

    # ---- 4. manifest freeze state ----------------------------------------
    auth = man.get("authorization_state", {})
    if auth.get("feature_parent_and_rp00_creation") is not True:
        err("E_AUTH_STATE", "feature_parent_and_rp00_creation 应为 true（GATE 链已放行）")
    if auth.get("rp01_through_rp10_creation") is not False:
        err("E_AUTH_STATE", "rp01_through_rp10_creation 应为 false（RP-00 未闭环前禁止）")

    fp = man.get("feature_parent", {})
    if fp.get("task_id", "").startswith("${"):
        err("E_FREEZE", "feature_parent.task_id 仍为占位符")
    parent_id = fp.get("task_id", "")

    cards = man.get("cards", [])
    rp_ids = [c.get("card_id", "") for c in cards]
    if rp_ids != [f"RP-{i:02d}" for i in range(len(cards))]:
        err("E_CARDS", f"cards 顺序异常: {rp_ids}")
    for card in cards:
        cid = card.get("card_id", "?")
        if cid == "RP-00":
            if card.get("task_id", "").startswith("${"):
                err("E_FREEZE", "RP-00 task_id 仍为占位符")
            if card.get("parent_task_id", "") != parent_id:
                err("E_FREEZE", "RP-00 parent_task_id 与 feature parent 不一致")
            pred = card.get("predecessor", {})
            if pred.get("task_id", "").startswith("${") or not pred.get("task_id"):
                err("E_FREEZE", "RP-00 predecessor (GATE-1B) task_id 未解析")
            if len(card.get("steps", [])) != 4:
                err("E_FREEZE", "RP-00 步骤数 != 4")
            if len(card.get("role_contracts", [])) != 3:
                err("E_FREEZE", "RP-00 role_contracts 数 != 3")
            if not card.get("predecessor", {}).get("required_lifecycle_status"):
                err("E_FREEZE", "RP-00 predecessor 释放条件缺失")
        else:
            # RP-01..RP-10: task id must still be an unresolved placeholder
            # (cards keep "task_id_placeholder" until their predecessor closes).
            if "task_id" in card and not str(card.get("task_id", "")).startswith("${"):
                err("E_FREEZE", f"{cid} task_id 已解析但其 predecessor 释放条件未触发")
            if card.get("parent_task_id", "") != parent_id:
                err("E_FREEZE", f"{cid} parent_task_id 与 feature parent 不一致")

    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="RP-00 frozen planning artifact validator")
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--resolution", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--repo-root", default=os.getcwd())
    args = ap.parse_args(argv)

    errors = validate(args.inventory, args.resolution, args.spec, args.manifest,
                      repo_root=args.repo_root)
    if errors:
        json.dump({"result": "FAIL", "errors": errors}, sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
        return 1
    json.dump({"result": "PASS", "errors": []}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
