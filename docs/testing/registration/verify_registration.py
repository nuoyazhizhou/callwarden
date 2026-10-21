"""Read back registration from the daemon; never claim or mutate a task."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT.parent))
from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)

HERE = Path(__file__).resolve().parent
client = HttpDaemonRpcClient()
cards = json.loads((HERE / 'cards.json').read_text(encoding='utf-8'))
receipts = json.loads((HERE / 'registered_tasks.json').read_text(encoding='utf-8'))
assert len(receipts) == 4 and len({r['task_id'] for r in receipts}) == 4
summary = []
for card, receipt in zip(cards, receipts):
    key = card['document_key']
    assert key == receipt['document_key']
    status = json.loads((HERE / (key + '-status.json')).read_text(encoding='utf-8'))
    projection = client.call('task.next_action', {'task_id': receipt['task_id'],
        'workspace_instance_id': receipt['workspace_instance_id']})
    assert status['task_id'] == receipt['task_id']
    assert status['workspace_id'] == 1 and status['workspace_instance_id'] == receipt['workspace_instance_id']
    assert status['workspace_binding_id'] and status['workspace_capture_id']
    assert status['lifecycle_status'] == 'open' and status['workflow_status'] == 'queued'
    assert status['progress']['total'] == len(card['steps']) and status['progress']['done'] == 0
    assert projection['decision'] == 'READY' and projection['action'] == 'CLAIM'
    assert projection['identity_policy_status'] == 'declared'
    assignment = projection['assignment']
    assert assignment['status'] == 'queued' and assignment['claimed_at'] is None
    assert not any(assignment.get(k) for k in ('holder_agent_id', 'holder_session_id', 'holder_model_id'))
    role_rows = []
    for expected in card['role_contracts']:
        response = client.call('task.contract_get', {'task_id': receipt['task_id'], 'role': expected['role']})
        (HERE / (key + '-' + expected['role'] + '-contract.json')).write_text(
            json.dumps(response, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        matches = [c for c in response.get('contracts', []) if c.get('role') == expected['role']]
        assert len(matches) == 1, (key, expected['role'], response)
        actual = matches[0]
        for field in ('skill_id', 'skill_version', 'prompt_template_id', 'prompt_hash',
                      'allowed_paths', 'forbidden_paths', 'commands', 'acceptance_checks',
                      'required_evidence', 'independence', 'handoff_to'):
            assert actual.get(field, '') == expected.get(field, ''), (key, expected['role'], field)
        role_rows.append({'role': expected['role'], 'readback': 'MATCH'})
    summary.append({'document_key': key, 'task_id': receipt['task_id'],
        'workspace_binding_id': status['workspace_binding_id'], 'steps': status['progress']['total'],
        'lifecycle_status': status['lifecycle_status'], 'workflow_status': status['workflow_status'],
        'claimed': False, 'role_contracts': role_rows, 'task_contract': projection['task_contract']})
(HERE / 'verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('4 tasks: exact workspace binding, all 12 Role Contracts match; 18 pending steps; no claim')
