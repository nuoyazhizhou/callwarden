# 测试矩阵 3：FTS5 跨语言符号搜索报告

> 执行时间：2026-07-16 15:13:24
> 项目数：15
> FTS5 索引正常：15 / 异常：0

## FTS5 索引状态 + 通用关键词搜索

| 语言 | 项目 | symbols | consistent | init | handle | parse | create | config | error | main |
|------|------|---------|------------|------|------|------|------|------|------|------|
| rust | bat | 1492 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| typescript | deno_std | 3842 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 3 |
| javascript | express | 123 | ✓ | 1 | 3 | 2 | 5 | 3 | 5 | 1 |
| python | flask | 1620 | ✓ | 5 | 5 | 1 | 5 | 5 | 5 | 5 |
| go | cobra | 617 | ✓ | 5 | 2 | 5 | 0 | 5 | 5 | 0 |
| java | guava | 66034 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| c | curl | 3588 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| cpp | fmt | 1009 | ✓ | 5 | 5 | 5 | 5 | 0 | 3 | 5 |
| csharp | Avalonia | 36508 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| ruby | rubocop | 10755 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| php | composer | 5081 | ✓ | 5 | 5 | 5 | 5 | 5 | 5 | 5 |
| swift | Alamofire | 2477 | ✓ | 5 | 5 | 1 | 5 | 5 | 5 | 5 |
| scala | cats | 11230 | ✓ | 5 | 5 | 5 | 5 | 3 | 5 | 5 |
| hcl | terraform_aws_vpc | 1804 | ✓ | 0 | 0 | 0 | 5 | 0 | 0 | 5 |
| elixir | ecto | 2854 | ✓ | 5 | 4 | 5 | 5 | 4 | 5 | 5 |

## 语言特定关键词搜索

### rust - bat

| 关键词 | 搜索结果数 |
|--------|-----------|
| fn | 0 |
| impl | 5 |
| trait | 0 |

### typescript - deno_std

| 关键词 | 搜索结果数 |
|--------|-----------|
| interface | 3 |
| async | 5 |
| await | 0 |

### javascript - express

| 关键词 | 搜索结果数 |
|--------|-----------|
| function | 0 |
| export | 0 |
| require | 0 |

### python - flask

| 关键词 | 搜索结果数 |
|--------|-----------|
| def | 5 |
| class | 5 |
| import | 5 |

### go - cobra

| 关键词 | 搜索结果数 |
|--------|-----------|
| func | 5 |
| package | 0 |
| interface | 0 |

### java - guava

| 关键词 | 搜索结果数 |
|--------|-----------|
| public | 5 |
| static | 5 |
| void | 5 |

### c - curl

| 关键词 | 搜索结果数 |
|--------|-----------|
| struct | 4 |
| typedef | 0 |
| include | 0 |

### cpp - fmt

| 关键词 | 搜索结果数 |
|--------|-----------|
| namespace | 0 |
| template | 1 |
| class | 3 |

### csharp - Avalonia

| 关键词 | 搜索结果数 |
|--------|-----------|
| namespace | 5 |
| public | 5 |
| static | 5 |

### ruby - rubocop

| 关键词 | 搜索结果数 |
|--------|-----------|
| def | 5 |
| module | 5 |
| require | 5 |

### php - composer

| 关键词 | 搜索结果数 |
|--------|-----------|
| function | 5 |
| class | 5 |
| namespace | 5 |

### swift - Alamofire

| 关键词 | 搜索结果数 |
|--------|-----------|
| func | 0 |
| struct | 5 |
| let | 5 |

### scala - cats

| 关键词 | 搜索结果数 |
|--------|-----------|
| def | 5 |
| object | 5 |
| trait | 5 |

### hcl - terraform_aws_vpc

| 关键词 | 搜索结果数 |
|--------|-----------|
| resource | 5 |
| variable | 5 |
| output | 5 |

### elixir - ecto

| 关键词 | 搜索结果数 |
|--------|-----------|
| def | 5 |
| defp | 0 |
| defmodule | 0 |

## 汇总统计

- 总项目数：15
- FTS5 索引一致：15 / 15
- 总符号数：149034
- 搜索查询总数：150
- 零结果查询数：22（14.7%）

## trigram 分词验证

trigram tokenizer 自动分词 snake_case / camelCase / `::` 路径，
搜索 `init` 应命中 `initialize` / `init_config` / `__init__` 等。
搜索 `handle` 应命中 `handleRequest` / `handle_error` / `EventHandler` 等。
