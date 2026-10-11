"""CLI argv adapter: avoid shell JSON quoting; all writes still use cw.py CLI."""
import json
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[3]
key = sys.argv[1]
cards = json.loads((Path(__file__).parent / "cards.json").read_text(encoding="utf-8"))
card = next(c for c in cards if c["document_key"] == key)
sys.argv = [str(root / "cw.py"), "task", "create", "--title", card["title"],
            "--desc", card["description"], "--steps", json.dumps(card["steps"], ensure_ascii=False),
            "--role-contracts", json.dumps(card["role_contracts"], ensure_ascii=False),
            "--identity-policy", card["identity_policy"],
            "--workspace-id", str(card["workspace_id"]),
            "--workspace-instance-id", card["workspace_instance_id"]]
runpy.run_path(str(root / "cw.py"), run_name="__main__")
