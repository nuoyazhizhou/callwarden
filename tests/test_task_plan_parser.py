"""task_create_from_plan 解析器鲁棒性测试

覆盖所有格式变体，逐个测试确保不崩溃。
测试策略：直接调用解析逻辑（不写数据库），验证解析结果正确。
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_plan(plan_md: str):
    """独立解析函数，模拟 task_create_from_plan 的解析逻辑，不写数据库。

    返回: (root_desc, root_steps, subtasks_def)
    """
    re_h1 = re.compile(r'^#\s+(.+?)\s*#*\s*$')
    re_h2 = re.compile(r'^##\s+(.+?)\s*#*\s*$')
    re_h3 = re.compile(r'^###\s+(.+?)\s*#*\s*$')
    re_h4plus = re.compile(r'^####+\s+(.+?)\s*#*\s*$')

    re_list = re.compile(
        r'^[-*+]\s+'
        r'(?:\[[ xX]\]\s+)?'
        r'(.+)$'
    )
    re_ordered = re.compile(
        r'^\d+\.\s+'
        r'(?:\[[ xX]\]\s+)?'
        r'(.+)$'
    )

    lines = plan_md.strip().split("\n")
    root_steps = []
    subtasks_def = []
    current_h2_title = None
    current_h2_desc_lines = []
    current_h2_steps = []
    in_h1_section = False
    h1_desc_lines = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if not stripped:
            continue

        m = re_h1.match(stripped)
        if m and not stripped.startswith("## "):
            in_h1_section = True
            continue

        m = re_h2.match(stripped)
        if m and not stripped.startswith("### "):
            if current_h2_title is not None:
                subtasks_def.append({
                    "title": current_h2_title,
                    "description": "\n".join(current_h2_desc_lines).strip(),
                    "steps": current_h2_steps,
                })
            current_h2_title = m.group(1).strip()
            current_h2_desc_lines = []
            current_h2_steps = []
            continue

        m = re_h3.match(stripped)
        if m and not stripped.startswith("#### "):
            if current_h2_title is not None:
                current_h2_steps.append({
                    "action": "group",
                    "check_items": [m.group(1).strip()],
                })
            continue

        m = re_h4plus.match(stripped)
        if m:
            if current_h2_title is not None:
                current_h2_steps.append({
                    "action": "group",
                    "check_items": [m.group(1).strip()],
                })
            continue

        m = re_list.match(stripped)
        if m:
            item_text = m.group(1).strip()
            step = {"action": "todo", "check_items": [item_text] if item_text else []}
            if current_h2_title is not None:
                current_h2_steps.append(step)
            elif in_h1_section:
                root_steps.append(step)
            continue

        m = re_ordered.match(stripped)
        if m:
            item_text = m.group(1).strip()
            step = {"action": "todo", "check_items": [item_text] if item_text else []}
            if current_h2_title is not None:
                current_h2_steps.append(step)
            elif in_h1_section:
                root_steps.append(step)
            continue

        if current_h2_title is not None:
            current_h2_desc_lines.append(stripped)
        elif in_h1_section:
            h1_desc_lines.append(stripped)

    if current_h2_title is not None:
        subtasks_def.append({
            "title": current_h2_title,
            "description": "\n".join(current_h2_desc_lines).strip(),
            "steps": current_h2_steps,
        })

    root_desc = "\n".join(h1_desc_lines).strip()
    return root_desc, root_steps, subtasks_def


# ============================================================
# 测试辅助函数
# ============================================================
def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}\n  期望: {expected}\n  实际: {actual}")


def get_step_texts(steps):
    """提取步骤的 check_items 文本列表"""
    return [s["check_items"][0] if s.get("check_items") else "" for s in steps]


# ============================================================
# 测试用例
# ============================================================

def test_basic_dash():
    """测试1: 基本 - 列表项"""
    plan = """# 根任务
## 子任务A
- 步骤1
- 步骤2
- 步骤3
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(subtasks[0]["title"], "子任务A", "子任务标题")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["步骤1", "步骤2", "步骤3"], "步骤内容")
    print("  ✓ 基本 - 列表项")


def test_star_list():
    """测试2: * 星号列表项"""
    plan = """# 根任务
## 子任务A
* 步骤1
* 步骤2
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["步骤1", "步骤2"], "步骤内容")
    print("  ✓ * 星号列表项")


def test_plus_list():
    """测试3: + 加号列表项"""
    plan = """# 根任务
## 子任务A
+ 步骤1
+ 步骤2
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["步骤1", "步骤2"], "步骤内容")
    print("  ✓ + 加号列表项")


def test_ordered_list():
    """测试4: 有序列表 1. 2. 3."""
    plan = """# 根任务
## 子任务A
1. 第一步
2. 第二步
3. 第三步
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["第一步", "第二步", "第三步"], "步骤内容")
    print("  ✓ 有序列表 1. 2. 3.")


def test_checkbox_dash():
    """测试5: checkbox - [ ] / - [x]"""
    plan = """# 根任务
## 子任务A
- [ ] 未完成步骤
- [x] 已完成步骤
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["未完成步骤", "已完成步骤"], "步骤内容")
    print("  ✓ checkbox - [ ] / - [x]")


def test_checkbox_star():
    """测试6: checkbox * [ ] / * [x]"""
    plan = """# 根任务
## 子任务A
* [ ] 未完成步骤
* [x] 已完成步骤
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["未完成步骤", "已完成步骤"], "步骤内容")
    print("  ✓ checkbox * [ ] / * [x]")


def test_checkbox_plus():
    """测试7: checkbox + [ ] / + [x]"""
    plan = """# 根任务
## 子任务A
+ [ ] 未完成步骤
+ [x] 已完成步骤
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["未完成步骤", "已完成步骤"], "步骤内容")
    print("  ✓ checkbox + [ ] / + [x]")


def test_checkbox_uppercase():
    """测试8: checkbox 大写 [X]"""
    plan = """# 根任务
## 子任务A
- [X] 大写X的checkbox
- [ ] 小写x的checkbox
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["大写X的checkbox", "小写x的checkbox"], "步骤内容")
    print("  ✓ checkbox 大写 [X]")


def test_indented_list():
    """测试9: 缩进列表项"""
    plan = """# 根任务
## 子任务A
  - 缩进2空格
  	- 缩进tab
- 无缩进
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    # 缩进的列表项也被解析（因为 stripped 去掉了缩进）
    assert_eq(len(subtasks[0]["steps"]), 3, "步骤数（含缩进）")
    print("  ✓ 缩进列表项")


def test_title_trailing_hash():
    """测试10: 标题末尾 # 字符"""
    plan = """# 根任务 #
## 子任务A ##
### 分组 ###
- 步骤1
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(subtasks[0]["title"], "子任务A", "子任务标题（去掉末尾#）")
    # 分组 ### 也应该去掉末尾 #
    group_steps = [s for s in subtasks[0]["steps"] if s["action"] == "group"]
    assert_eq(len(group_steps), 1, "分组数量")
    assert_eq(group_steps[0]["check_items"][0], "分组", "分组标题（去掉末尾#）")
    print("  ✓ 标题末尾 # 字符")


def test_code_block():
    """测试11: 代码块内容不解析"""
    plan = """# 根任务
## 子任务A
- 正常步骤

```
## 这不是子任务
- 这不是步骤
```

## 子任务B
- 步骤B
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 2, "子任务数（代码块内不算）")
    assert_eq(subtasks[0]["title"], "子任务A", "第一个子任务")
    assert_eq(subtasks[1]["title"], "子任务B", "第二个子任务")
    print("  ✓ 代码块内容不解析")


def test_tilde_code_block():
    """测试12: ~~~ 围栏代码块"""
    plan = """# 根任务
## 子任务A
- 正常步骤

~~~
- 这不是步骤
~~~

## 子任务B
- 步骤B
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 2, "子任务数")
    assert_eq(len(subtasks[0]["steps"]), 1, "子任务A步骤数（~~~内不算）")
    print("  ✓ ~~~ 围栏代码块")


def test_no_subtasks():
    """测试13: 只有根任务无子任务"""
    plan = """# 根任务
这是描述

- 根步骤1
- 根步骤2
"""
    root_desc, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 0, "子任务数")
    assert_eq(len(root_steps), 2, "根步骤数")
    assert_eq(get_step_texts(root_steps), ["根步骤1", "根步骤2"], "根步骤内容")
    assert_eq(root_desc, "这是描述", "根描述")
    print("  ✓ 只有根任务无子任务")


def test_empty_subtask_steps():
    """测试14: 子任务无步骤（自动补默认步骤）"""
    plan = """# 根任务
## 子任务A（无步骤）

## 子任务B
- 步骤B
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 2, "子任务数")
    assert_eq(len(subtasks[0]["steps"]), 0, "子任务A步骤数（原始为0）")
    assert_eq(len(subtasks[1]["steps"]), 1, "子任务B步骤数")
    print("  ✓ 子任务无步骤")


def test_mixed_format():
    """测试15: 混合格式"""
    plan = """# 混合格式任务
## 子任务A
- dash格式步骤
* star格式步骤
+ plus格式步骤
1. 有序格式步骤
- [ ] checkbox步骤

## 子任务B
2. 第二步
* [x] 已完成
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 2, "子任务数")
    assert_eq(len(subtasks[0]["steps"]), 5, "子任务A步骤数")
    assert_eq(len(subtasks[1]["steps"]), 2, "子任务B步骤数")
    print("  ✓ 混合格式")


def test_chinese_content():
    """测试16: 中文内容"""
    plan = """# 性能优化专项
对系统核心模块进行性能优化

## 1. 数据库查询优化
分析慢查询，添加索引

- 开启慢查询日志
- 为高频查询字段添加索引
- 优化 N+1 查询

## 2. 验证测试
- 运行全部测试
- 性能基准对比
"""
    root_desc, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 2, "子任务数")
    assert_eq(subtasks[0]["title"], "1. 数据库查询优化", "中文子任务标题")
    assert_eq(subtasks[0]["description"], "分析慢查询，添加索引", "中文描述")
    assert_eq(len(subtasks[0]["steps"]), 3, "中文步骤数")
    assert_eq(get_step_texts(subtasks[0]["steps"]),
              ["开启慢查询日志", "为高频查询字段添加索引", "优化 N+1 查询"], "中文步骤内容")
    print("  ✓ 中文内容")


def test_h3_group():
    """测试17: ### 三级标题作为步骤分组"""
    plan = """# 根任务
## 子任务A
### 分组1
- 步骤1
- 步骤2
### 分组2
- 步骤3
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    groups = [s for s in subtasks[0]["steps"] if s["action"] == "group"]
    assert_eq(len(groups), 2, "分组数")
    assert_eq(groups[0]["check_items"][0], "分组1", "第一个分组")
    assert_eq(groups[1]["check_items"][0], "分组2", "第二个分组")
    print("  ✓ ### 三级标题作为步骤分组")


def test_h4_group():
    """测试18: #### 四级标题降级为分组"""
    plan = """# 根任务
## 子任务A
#### 四级标题
- 步骤1
"""
    _, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 1, "子任务数")
    groups = [s for s in subtasks[0]["steps"] if s["action"] == "group"]
    assert_eq(len(groups), 1, "分组数")
    assert_eq(groups[0]["check_items"][0], "四级标题", "四级标题分组")
    print("  ✓ #### 四级标题降级为分组")


def test_empty_plan():
    """测试19: 空计划容错"""
    plan = ""
    root_desc, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 0, "子任务数")
    assert_eq(len(root_steps), 0, "根步骤数")
    print("  ✓ 空计划容错")


def test_whitespace_only():
    """测试20: 只有空行的计划"""
    plan = "   \n\n  \n"
    root_desc, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 0, "子任务数")
    print("  ✓ 只有空行容错")


def test_no_h1():
    """测试21: 没有一级标题"""
    plan = """## 子任务A
- 步骤1

## 子任务B
- 步骤2
"""
    root_desc, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(subtasks), 2, "子任务数")
    assert_eq(get_step_texts(subtasks[0]["steps"]), ["步骤1"], "子任务A步骤")
    print("  ✓ 没有一级标题")


def test_multiple_root_steps():
    """测试22: 根任务有多个步骤"""
    plan = """# 根任务
描述

- 根步骤1
- 根步骤2
- 根步骤3

## 子任务A
- 子步骤1
"""
    root_desc, root_steps, subtasks = parse_plan(plan)
    assert_eq(len(root_steps), 3, "根步骤数")
    assert_eq(len(subtasks), 1, "子任务数")
    print("  ✓ 根任务有多个步骤")


# ============================================================
# 主函数：逐个运行测试
# ============================================================
def main():
    tests = [
        ("基本 - 列表项", test_basic_dash),
        ("* 星号列表项", test_star_list),
        ("+ 加号列表项", test_plus_list),
        ("有序列表 1. 2. 3.", test_ordered_list),
        ("checkbox - [ ] / - [x]", test_checkbox_dash),
        ("checkbox * [ ] / * [x]", test_checkbox_star),
        ("checkbox + [ ] / + [x]", test_checkbox_plus),
        ("checkbox 大写 [X]", test_checkbox_uppercase),
        ("缩进列表项", test_indented_list),
        ("标题末尾 # 字符", test_title_trailing_hash),
        ("代码块内容不解析", test_code_block),
        ("~~~ 围栏代码块", test_tilde_code_block),
        ("只有根任务无子任务", test_no_subtasks),
        ("子任务无步骤", test_empty_subtask_steps),
        ("混合格式", test_mixed_format),
        ("中文内容", test_chinese_content),
        ("### 三级标题分组", test_h3_group),
        ("#### 四级标题降级", test_h4_group),
        ("空计划容错", test_empty_plan),
        ("只有空行容错", test_whitespace_only),
        ("没有一级标题", test_no_h1),
        ("根任务多步骤", test_multiple_root_steps),
    ]

    passed = 0
    failed = 0
    errors = []

    print("=" * 60)
    print(f"task_create_from_plan 解析器鲁棒性测试 ({len(tests)} 个用例)")
    print("=" * 60)

    for name, func in tests:
        try:
            func()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"  ✗ {name}: {e}")

    print()
    print("=" * 60)
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 个")
    print("=" * 60)

    if errors:
        print("\n失败详情:")
        for name, err in errors:
            print(f"  - {name}: {err}")
        return 1
    else:
        print("\n✓ 全部测试通过！")
        return 0


if __name__ == "__main__":
    sys.exit(main())
