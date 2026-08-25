"""从 server authority 残留审计生成 A′ SRV 模块迁移卡；不修改任务库。"""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "deliverables" / "software-company" / "server_authority_residue_audit.json"
OUT = ROOT / "deliverables" / "software-company" / "aprime_server_authority_manifest.json"
PARENT_ID = "T-1787293451688-c14b1e44"
EXCLUDED_PREFIXES = {"server/tools/"}
EXCLUDED_FILES = {"server/compat_registry.py", "server/compat_worker.py"}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def subsystem(file: str) -> str:
    return Path(file).stem.replace("_", " ")


def port_type(file: str) -> str:
    if file in {"server/daemon_client.py", "server/daemon_autostart.py", "server/daemon_server.py", "server/daemon_protocol.py"}:
        return "control_plane"
    if any(name in file for name in ("audit", "staging", "schema", "backup", "snapshot", "replicator")):
        return "governance_projection"
    if any(name in file for name in ("job", "health", "metrics", "budget")):
        return "runtime_projection"
    return "service_projection"


def rust_targets(file: str, handlers: list[str]) -> list[str]:
    stem = Path(file).stem
    module = f"rust_ext/src/daemon/{stem}_handlers.rs"
    targets = []
    for handler in handlers:
        clean = slug(handler.lstrip("_"))
        targets.append(f"{module}::handle_{clean}")
    return targets


def main() -> None:
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    grouped: dict[str, list[dict]] = {}
    for finding in audit["findings"]:
        file = finding.get("file", "")
        if not file or any(file.startswith(prefix) for prefix in EXCLUDED_PREFIXES) or file in EXCLUDED_FILES:
            continue
        grouped.setdefault(file, []).append(finding)
    cards = []
    for index, (file, findings) in enumerate(sorted(grouped.items()), start=1):
        handlers = sorted({item.get("handler", "<module>") for item in findings})
        calls = sorted({item.get("call", "") for item in findings})
        cards.append({
            "card_key": f"SRV-{index:03d}", "python_file": file, "subsystem": subsystem(file),
            "handlers": handlers, "calls": calls, "lines": sorted({item.get("line", 0) for item in findings if item.get("line")}),
            "port_type": port_type(file), "rust_targets": rust_targets(file, handlers),
            "execution_dependency": "CLI-01 applied；本卡预建不等于可领取。若依赖现有 daemon transport/authority card，则在其 applied 后领取。",
        })
    cards.append({
        "card_key": f"SRV-{len(cards)+1:03d}", "python_file": "repository-wide", "subsystem": "Python authority zero-residue final gate",
        "handlers": ["final_zero_python_authority_audit"], "calls": ["AST/grep/negative runtime probes"], "lines": [],
        "port_type": "control_plane", "rust_targets": ["rust_ext/src/daemon/dispatch.rs::dispatch_rpc", "rust_ext/src/daemon/http_server.rs::capability registry"],
        "execution_dependency": "所有 SRV、CLI、MCP 与 INT 卡均 applied；本卡是最终 Gate，不得在此前声称 Python 已无业务 authority。",
    })
    OUT.write_text(json.dumps({"format": "cw.aprime.server-authority.v1", "parent_id": PARENT_ID, "source": str(AUDIT.relative_to(ROOT)).replace("\\", "/"), "card_count": len(cards), "cards": cards}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"card_count": len(cards), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
