# 测试矩阵 1：解析器 + 调用链验证报告

> 执行时间：2026-07-16 14:51:02
> 项目数：32
> 成功：32 / 失败：0

## 按语言汇总

| 语言 | 项目 | 文件数 | 符号数 | 调用数 | 解析耗时 | 状态 | 抽样符号 | callers | callees |
|------|------|--------|--------|--------|----------|------|----------|---------|---------|
| rust | bat | 99 | 1485 | 9072 | 0.4s | OK | lib::assets::theme_preview.pri | 0 | 1 |
| rust | ripgrep | 101 | 1175 | 2983 | 0.3s | OK | lib::build.main | 0 | 2 |
| typescript | deno_std | 1235 | 3689 | 14027 | 0.6s | OK | _tools.node_test_runner.deno_c | 1 | 9 |
| typescript | typeorm | 3574 | 5132 | 15576 | 1.5s | OK | docs.src.pages.HomepageHeader | 0 | 2 |
| javascript | chalk | 13 | 15 | 31 | 0.3s | OK | examples.rainbow.rainbow | 1 | 8 |
| javascript | express | 141 | 91 | 332 | 0.3s | OK | examples.auth.authenticate | 0 | 6 |
| python | flask | 83 | 1496 | 3724 | 0.3s | OK | examples.celery.src.task_app.c | 4 | 7 |
| python | requests | 37 | 787 | 2510 | 0.3s | OK | docs._themes.flask_theme_suppo | 0 | 0 |
| kotlin | kotlinx_coroutines | 1105 | 7620 | 27370 | 0.7s | OK | benchmarks.src.jmh.kotlin.benc | 0 | 0 |
| kotlin | ktor | 2504 | 15925 | 43714 | 1.9s | OK | build-logic.src.main.kotlin.kt | 0 | 0 |
| go | cobra | 36 | 611 | 4428 | 0.3s | OK | active_help.AppendActiveHelp | 11 | 2 |
| go | gin | 98 | 1492 | 9158 | 0.3s | OK | auth.Accounts | 0 | 0 |
| java | guava | 3226 | 33018 | 259654 | 2.3s | OK | android.guava-testlib.src.com. | 0 | 0 |
| java | retrofit | 357 | 2626 | 18078 | 0.5s | OK | retrofit-adapters.guava.src.ma | 1 | 0 |
| c | curl | 1055 | 3466 | 26244 | 0.7s | OK | docs.examples.10-at-a-time.wri | 0 | 51 |
| c | redis | 329 | 5611 | 39921 | 0.5s | OK | modules.vector-sets.expr.exprs | 0 | 0 |
| cpp | fmt | 77 | 994 | 10196 | 0.3s | OK | include.fmt.chrono.write_nan | 0 | 1 |
| cpp | spdlog | 152 | 175 | 651 | 0.3s | OK | include.spdlog.async.init_thre | 2 | 2 |
| csharp | Avalonia | 4112 | 35516 | 19921 | 1.6s | OK | nukebuild.ApiDiffHelper.ApiDif | 0 | 0 |
| csharp | csharplang | 3 | 19 | 3 | 0.3s | OK | proposals.csharp-8.0.ranges.In | 0 | 1 |
| ruby | rubocop | 1722 | 10613 | 34090 | 0.8s | OK | rubocop.arguments_env.RuboCop | 0 | 0 |
| ruby | sinatra | 147 | 1125 | 2810 | 0.3s | OK | sinatra.base.Sinatra | 0 | 0 |
| php | composer | 589 | 4960 | 32246 | 0.6s | OK | Composer.Advisory.AuditConfig. | 0 | 0 |
| php | monolog | 217 | 1568 | 6269 | 0.3s | OK | Monolog.Attribute.AsMonologPro | 0 | 0 |
| swift | Alamofire | 103 | 2445 | 757 | 0.3s | OK | Example.Source.AppDelegate.App | 0 | 0 |
| swift | vapor | 251 | 2042 | 69 | 0.3s | OK | Sources.Development.configure. | 0 | 0 |
| scala | cats | 819 | 10728 | 777 | 0.6s | OK | algebra-core.src.main.scala.al | 0 | 0 |
| scala | playframework | 1622 | 15322 | 32951 | 4.9s | OK | cache.play-cache.src.main.java | 0 | 0 |
| hcl | terraform_aws_security_group | 441 | 186 | 4806 | 0.3s | OK | examples.complete.main.aws | 0 | 1 |
| hcl | terraform_aws_vpc | 77 | 827 | 7352 | 0.3s | OK | examples.block-public-access.m | 0 | 1 |
| elixir | ecto | 126 | 2723 | 3589 | 0.3s | OK | examples.friends.lib.friends.F | 0 | 0 |
| elixir | phoenix | 199 | 2252 | 3748 | 0.3s | OK | installer.lib.mix.tasks.local. | 0 | 0 |

## 汇总统计

- 总文件数：24650
- 总符号数：175734
- 总调用数：637057
- 成功项目：32/32
- 失败项目：0

## 语言覆盖验证

- rust: 2 项目, 2660 符号
- typescript: 2 项目, 8821 符号
- javascript: 2 项目, 106 符号
- python: 2 项目, 2283 符号
- kotlin: 2 项目, 23545 符号
- go: 2 项目, 2103 符号
- java: 2 项目, 35644 符号
- c: 2 项目, 9077 符号
- cpp: 2 项目, 1169 符号
- csharp: 2 项目, 35535 符号
- ruby: 2 项目, 11738 符号
- php: 2 项目, 6528 符号
- swift: 2 项目, 4487 符号
- scala: 2 项目, 26050 符号
- hcl: 2 项目, 1013 符号
- elixir: 2 项目, 4975 符号