# -*- coding: utf-8 -*-
"""RP-00 validator tests.

Covers:
  - the real in-repo frozen artifacts pass end-to-end
  - synthetic negative cases: spec hash mismatch, coverage gap, duplicate
    resolution, out-of-set disposition, freeze-state violations
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VALIDATOR = os.path.join(REPO_ROOT, "scripts", "validate_role_prompt_spec.py")

REAL_INV = os.path.join(REPO_ROOT, "docs", "evidence", "RP-00-source-clause-inventory.json")
REAL_MAT = os.path.join(REPO_ROOT, "docs", "evidence", "RP-00-review-resolution-matrix.json")
REAL_SPEC = os.path.join(REPO_ROOT, "docs", "design", "cw-role-prompt-compiler-v1-frozen-spec.md")
REAL_MAN = os.path.join(REPO_ROOT, "deliverables", "software-company", "role-prompt-v1-task-manifest.json")


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_role_prompt_spec", VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def validator():
    return _load_module()


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, VALIDATOR] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=REPO_ROOT,
    )


def _sha(path):
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest().upper()


# --------------------------------------------------------------------------
# real artifacts
# --------------------------------------------------------------------------

def test_real_artifacts_pass(validator):
    errors = validator.validate(REAL_INV, REAL_MAT, REAL_SPEC, REAL_MAN, repo_root=REPO_ROOT)
    assert errors == [], f"真实冻结产物应全部通过，实际: {errors}"


def test_cli_exit_zero_on_real_artifacts():
    proc = _run_cli(
        "--inventory", REAL_INV, "--resolution", REAL_MAT,
        "--spec", REAL_SPEC, "--manifest", REAL_MAN,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"result": "PASS"' in proc.stdout


# --------------------------------------------------------------------------
# synthetic negative fixtures
# --------------------------------------------------------------------------

def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _fake_bundle(tmp_path, spec_text="# frozen spec v1\n"):
    spec = tmp_path / "spec.md"
    spec.write_text(spec_text, encoding="utf-8")
    spec_sha = _sha(str(spec))

    clauses = [{"clause_id": f"C{i:03d}", "source_path": "spec.md", "source_sha256": spec_sha,
                "line_start": i + 1, "section": "s", "kind": "paragraph"} for i in range(3)]
    inv = {"frozen_spec": {"sha256": spec_sha}, "clause_count": 3,
           "sources": [{"clause_count": 3}], "clauses": clauses}
    allowed = ["retained", "superseded", "rejected", "deferred"]
    mat = {"frozen_spec": {"sha256": spec_sha},
           "inventory": {"sha256": _sha(_write(tmp_path, "inv.json", inv))},
           "allowed_dispositions": allowed,
           "resolution_count": 3,
           "resolutions": [{"clause_id": c["clause_id"], "disposition": "retained"} for c in clauses]}
    inv_p = _write(tmp_path, "inv.json", inv)
    mat["inventory"]["sha256"] = _sha(inv_p)
    mat_p = _write(tmp_path, "mat.json", mat)

    man = {
        "source": {"frozen_spec_sha256": spec_sha},
        "companion_artifacts": {
            "source_clause_inventory": {"sha256": _sha(inv_p)},
            "review_resolution_matrix": {"sha256": _sha(mat_p)},
        },
        "authorization_state": {"feature_parent_and_rp00_creation": True,
                                 "rp01_through_rp10_creation": False},
        "feature_parent": {"task_id": "T-1"},
        "cards": [
            {"card_id": "RP-00", "task_id": "T-2", "parent_task_id": "T-1",
             "predecessor": {"task_id": "T-0", "required_lifecycle_status": "closed"},
             "steps": [1, 2, 3, 4], "role_contracts": [1, 2, 3]},
            {"card_id": "RP-01", "task_id": "${RP01_TASK_ID}", "parent_task_id": "T-1"},
        ],
    }
    man_p = _write(tmp_path, "man.json", man)
    return inv_p, mat_p, str(spec), man_p


def test_synthetic_bundle_passes(tmp_path, validator):
    inv, mat, spec, man = _fake_bundle(tmp_path)
    assert validator.validate(inv, mat, spec, man) == []


def test_spec_hash_mismatch(tmp_path, validator):
    inv, mat, spec, man = _fake_bundle(tmp_path)
    man_obj = json.load(open(man, encoding="utf-8"))
    man_obj["source"]["frozen_spec_sha256"] = "0" * 64
    man2 = _write(tmp_path, "man2.json", man_obj)
    errors = validator.validate(inv, mat, spec, man2)
    assert any(e["code"] == "E_SPEC_HASH" for e in errors)


def test_coverage_gap(tmp_path, validator):
    inv, mat, spec, man = _fake_bundle(tmp_path)
    mat_obj = json.load(open(mat, encoding="utf-8"))
    mat_obj["resolutions"] = mat_obj["resolutions"][:2]
    mat_obj["resolution_count"] = 2
    mat2 = _write(tmp_path, "mat2.json", mat_obj)
    man_obj = json.load(open(man, encoding="utf-8"))
    man_obj["companion_artifacts"]["review_resolution_matrix"]["sha256"] = _sha(mat2)
    man2 = _write(tmp_path, "man3.json", man_obj)
    errors = validator.validate(inv, mat2, spec, man2)
    assert any(e["code"] == "E_COVERAGE_GAP" for e in errors)


def test_duplicate_resolution(tmp_path, validator):
    inv, mat, spec, man = _fake_bundle(tmp_path)
    mat_obj = json.load(open(mat, encoding="utf-8"))
    mat_obj["resolutions"].append(dict(mat_obj["resolutions"][0]))
    mat_obj["resolution_count"] = 4
    mat2 = _write(tmp_path, "mat2.json", mat_obj)
    errors = validator.validate(inv, mat2, spec, man)
    assert any(e["code"] == "E_MATRIX_DUP" for e in errors)


def test_disposition_outside_allowed(tmp_path, validator):
    inv, mat, spec, man = _fake_bundle(tmp_path)
    mat_obj = json.load(open(mat, encoding="utf-8"))
    mat_obj["resolutions"][0]["disposition"] = "invented"
    mat2 = _write(tmp_path, "mat2.json", mat_obj)
    errors = validator.validate(inv, mat2, spec, man)
    assert any(e["code"] == "E_DISPOSITION" for e in errors)


def test_rp01_resolution_forbidden(tmp_path, validator):
    """RP-01+ task ids must stay placeholders while rp01_through_rp10_creation is false."""
    inv, mat, spec, man = _fake_bundle(tmp_path)
    man_obj = json.load(open(man, encoding="utf-8"))
    man_obj["cards"][1]["task_id"] = "T-99"  # prematurely resolved
    man2 = _write(tmp_path, "man4.json", man_obj)
    errors = validator.validate(inv, mat, spec, man2)
    assert any(e["code"] == "E_FREEZE" and "RP-01" in e["message"] for e in errors)
