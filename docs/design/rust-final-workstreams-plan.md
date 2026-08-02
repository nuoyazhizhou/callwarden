# Rust 最终迁移六个工作包

## W1 CLI 完整语义迁移

完成剩余 Rust `cw` 命令的真实语义、参数、i18n、退出码、错误码和 Python/Rust 进程差分。

## W2 Client Agent 与 daemon 闭环

完成 client/agent Slice 6/7、跨平台 transport、watcher、重连、恢复和真实多用户 E2E。

## W3 默认切换与 rollback 窗口

为所有 service 建立版本化 rollback feature，默认走 Rust，异常 fail closed，显式 rollback 才允许兼容路径。

## W4 删除 Python fallback 与死代码

删除 parser/storage/build/query/CAS/watcher/daemon 生产 fallback，加入 import 和冻结包门禁。

## W5 发布与企业证据

完成六平台包体、SHA256、SBOM、签名、升级/回滚、schema/backup/restore 和 CI smoke 证据。

## W6 最终 parity、灾备与独立复审

完成全量差分、性能、多用户、灾备、审计、文档同步，并交由独立 Reviewer 关闭任务。

