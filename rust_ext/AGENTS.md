<!-- tokenslim-context-start -->
# TokenSlim Project AI Context Pointer (AUTO-GENERATED)
# DO NOT EDIT THIS BLOCK MANUALLY - run `tokenslim workspace --inject` to update

Full TokenSlim workspace context lives in `.tokenslim-context.md`.
Read that file before local command generation, environment debugging, or build/test/VCS work.

Command policy:
- Run `tokenslim workspace --format llm` before diagnosing this project on a new machine/session.
- Use the `Detected Project Commands` section in `.tokenslim-context.md` as the source of truth.
- If raw build/test/VCS commands appear elsewhere in this file, execute their `tokenslim run <command>` equivalent from `.tokenslim-context.md`.
- Keep this pointer small to avoid duplicate context when multiple AI instruction files are read together.

<!-- tokenslim-context-end -->

# TokenSlim Command Policy (通用)

所有构建 / 测试 / VCS / 基础设施命令都通过 `tokenslim run <command>` 执行，以压缩输出、节省 token：

```bash
# 不要这样
npm test
# 应该这样
tokenslim run npm test
```

常用命令：
```bash
tokenslim run git status | log | diff
tokenslim run <build/test command from .tokenslim-context.md>
tokenslim workspace --format llm   # 新会话/新机器先跑，拿到真实本地环境
```

> 本仓库的 TokenSlim 工作区上下文由 `tokenslim workspace --inject` 自动生成并维护于 `.tokenslim-context.md`，请勿手改。
