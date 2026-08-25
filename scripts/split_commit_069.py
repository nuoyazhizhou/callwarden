"""MCP-069 分离提交：字节级重建 task_collab.rs 和 dispatch.rs。

文件 3-6 只有 MCP-069 变更，直接用 git add。
文件 1-2 含 P0-G hunk，需字节级重建：git hash-object -w --no-filters + git update-index。
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)


def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True)


def detect_nl(data: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in data else b"\n"


def adapt(data: bytes, target_nl: bytes) -> bytes:
    """统一行尾，保留末尾换行。"""
    data = data.replace(b"\r\n", b"\n")
    lines = data.split(b"\n")
    result = target_nl.join(lines)
    if data.endswith(b"\n"):
        result += target_nl
    return result


def get_head_bytes(rel_path):
    """获取 HEAD blob 的原始字节。"""
    raw = run(["git", "show", f"HEAD:{rel_path}"])
    data = raw.encode("utf-8", errors="replace")
    if not data.endswith(b"\n"):
        data += b"\n"
    return data


def get_wt_bytes(rel_path):
    with open(os.path.join(REPO, rel_path), "rb") as f:
        return f.read()


def find_line(data: bytes, anchor: str) -> int:
    """查找锚点所在行号（0-indexed）。"""
    norm = data.replace(b"\r\n", b"\n")
    ab = anchor.encode("utf-8")
    for i, line in enumerate(norm.split(b"\n")):
        if ab in line:
            return i
    raise ValueError(f"anchor not found: {anchor}")


def git_hash_object(data: bytes) -> str:
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--no-filters", "--stdin"],
        input=data, capture_output=True, cwd=REPO,
    )
    return proc.stdout.decode().strip()


def git_update_index(rel_path: str, blob_hash: str):
    run(["git", "update-index", "--add", "--cacheinfo", "100644", blob_hash, rel_path])


# ============================================================
# 1. rust_ext/src/daemon/task_collab.rs
#   追加 handle_get_clone_group_detail 函数（在 handle_list_clone_groups 之后）
# ============================================================
print("=== task_collab.rs ===")
head = get_head_bytes("rust_ext/src/daemon/task_collab.rs")
head_nl = detect_nl(head)

# 从工作树提取 MCP-069 handler 块
wt = get_wt_bytes("rust_ext/src/daemon/task_collab.rs")
wt_norm = wt.replace(b"\r\n", b"\n")
wt_lines = wt_norm.split(b"\n")

start_anchor = "    // MCP-069（T-1787321713485-f7a90848）：get_clone_group_detail 从 python_compat"
start_idx = find_line(wt_norm, start_anchor)

# 找到函数结束的 closing brace（在 start_anchor 之后，找到第二个 }）
brace_count = 0
end_idx = start_idx
for i in range(start_idx, len(wt_lines)):
    line = wt_lines[i].strip()
    if line == b"}":
        brace_count += 1
        if brace_count >= 2:
            end_idx = i
            break

# 提取完整块（含结尾）
new_block = b"\n".join(wt_lines[start_idx : end_idx + 1]) + b"\n"
print(f"  提取块: 行 {start_idx}-{end_idx} ({len(new_block)} bytes)")

# 在 HEAD 中，在 handle_list_clone_groups 之后、handle_list_clones 之前插入
insert_anchor = "    pub fn handle_list_clones("
insert_idx = find_line(head, insert_anchor)  # 在这个行号之前插入

head_norm = head.replace(b"\r\n", b"\n")
head_lines = head_norm.split(b"\n")
before = b"\n".join(head_lines[:insert_idx])
after = b"\n".join(head_lines[insert_idx:])

# 保证 before 和 after 末尾有换行
if before and not before.endswith(b"\n"):
    before += b"\n"
if after and not after.startswith(b"\n"):
    after = b"\n" + after

reconstructed = adapt(before, head_nl) + adapt(new_block, head_nl) + adapt(after, head_nl)

blob = git_hash_object(reconstructed)
git_update_index("rust_ext/src/daemon/task_collab.rs", blob)
print(f"  blob={blob}")


# ============================================================
# 2. rust_ext/src/daemon/dispatch.rs
#   添加 RPC 分支 + 白名单
# ============================================================
print("=== dispatch.rs ===")
head = get_head_bytes("rust_ext/src/daemon/dispatch.rs")
head_nl = detect_nl(head)
head_norm = head.replace(b"\r\n", b"\n")
head_lines = head_norm.split(b"\n")

wt = get_wt_bytes("rust_ext/src/daemon/dispatch.rs")
wt_norm = wt.replace(b"\r\n", b"\n")
wt_lines = wt_norm.split(b"\n")

# A. RPC 分支：在 list_clone_groups 分支之后插入
rpc_anchor = '                "list_clone_groups" => store.handle_list_clone_groups(peer, params),'
rpc_insert_idx = find_line(head_norm, rpc_anchor) + 1  # 在 anchor 行之后

# 从工作树提取 RPC 分支块
rpc_start = '                // MCP-069（T-1787321713485-f7a90848）：get_clone_group_detail 迁移 rust_native。'
rpc_end = '                "get_clone_group_detail" => store.handle_get_clone_group_detail(peer, params),'
rpc_start_idx = find_line(wt_norm, rpc_start)
rpc_end_idx = find_line(wt_norm, rpc_end)
rpc_block = b"\n".join(wt_lines[rpc_start_idx : rpc_end_idx + 1]) + b"\n"

# 在前半部分之后插入
before = b"\n".join(head_lines[:rpc_insert_idx])
after = b"\n".join(head_lines[rpc_insert_idx:])
if before and not before.endswith(b"\n"):
    before += b"\n"
reconstructed = adapt(before, head_nl) + adapt(rpc_block, head_nl) + adapt(after, head_nl)

# B. 白名单：在 list_clone_groups 白名单之后插入
# 用重建后的数据
recon_norm = reconstructed.replace(b"\r\n", b"\n")
recon_lines = recon_norm.split(b"\n")

wl_anchor = '        | "list_clone_groups"'
wl_insert_idx = find_line(recon_norm, wl_anchor) + 1  # 在 anchor 行之后

# 从工作树提取白名单行
wl_line = '        | "get_clone_group_detail"'
wl_block = wl_line.encode() + b"\n"

before2 = b"\n".join(recon_lines[:wl_insert_idx])
after2 = b"\n".join(recon_lines[wl_insert_idx:])
if before2 and not before2.endswith(b"\n"):
    before2 += b"\n"
reconstructed = adapt(before2, head_nl) + adapt(wl_block, head_nl) + adapt(after2, head_nl)

blob = git_hash_object(reconstructed)
git_update_index("rust_ext/src/daemon/dispatch.rs", blob)
print(f"  blob={blob}")


# ============================================================
# 3-6. 纯净文件 + 测试文件
# ============================================================
print("=== 添加纯净文件 ===")
clean = [
    "rust_ext/src/daemon/http_server.rs",
    "server/tools/tools_task.py",
    "server/compat_registry.py",
    "deliverables/software-company/tool_migration_matrix.json",
    "tests/test_mcp_get_clone_group_detail_http_rpc.py",
]
for f in clean:
    run(["git", "add", f])
    print(f"  {f}")

print("\n=== 完成。请执行 git commit ===")
print('git commit -m "MCP-069: get_clone_group_detail 迁移 Rust daemon native"')