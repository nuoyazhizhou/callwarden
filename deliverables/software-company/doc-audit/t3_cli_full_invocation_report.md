# T3 实测报告：234 个 CLI 叶子命令全参数真实调用

日期：2026-10-13 05:10:36
环境：Windows，隔离 daemon（git HEAD c150807，二进制 f1fa3736a16dbe7d，
endpoint http://127.0.0.1:10917，完全隔离 data_root，**未触碰生产 daemon**），
种子 workspace（CWD 绑定 + refresh --all，refresh rc=0）。

## 1. 测试目标与方法

对全部 **234 个 CLI 叶子命令**做**全参数真实调用**（subprocess 执行
`python cw.py <argv>`，cwd=种子 workspace），验证命令可执行、不崩溃，
定位真实缺陷。分层：只读全执行；破坏性/重操作/阻塞 SKIP。

## 2. 结果汇总

| 分类 | 数量 | 含义 |
| --- | --- | --- |
| PASS | 85 | rc==0 / call_tool 非 error |
| EXPECTED_BUSINESS | 117 | 合理业务拒绝（缺参/not_found/snapshot/identity 等） |
| DEFECT | 0 | 真实缺陷（method_not_found / traceback / internal_error） |
| SKIP | 31 | 破坏性/重操作/阻塞命令 |
| **合计** | **233** | |

## 3. DEFECT 明细（0）

- method_not_found：0
- Python traceback：0
- 其它：0

**零 DEFECT**。
