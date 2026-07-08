"""P15.1: 资源文件内容特征检测测试。

覆盖 _is_resource_file() 函数：
- LVGL 资源文件检测（LV_ATTRIBUTE_IMG_ / LV_ATTRIBUTE_LARGE_CONST 宏 + 字节数组）
- 十六进制字节数组密度检测（无 LVGL 宏但高密度 0x.. 字面量）
- 正常代码不误判（C / Python / Rust / 短文件 / 空文件）
- 文件不存在 / 无法读取
- _parse_file_worker 对资源文件返回 skip_resource
"""
import os
import sys
import tempfile

import pytest

from callwarden.db.db_build import (
    _is_resource_file,
    _parse_file_worker,
    _LVGL_RESOURCE_MARKERS,
    _HEX_LITERAL_RE,
    _HEX_DENSITY_THRESHOLD,
    _HEX_MIN_LINES,
)


# ============================================
# 测试用样本：LVGL 资源文件（字体/图片数据的 C 数组）
# ============================================

LVGL_RESOURCE_SAMPLE = """#ifdef __has_include
    #if __has_include("lvgl.h")
        #ifndef LV_LVGL_H_INCLUDE_SIMPLE
            #define LV_LVGL_H_INCLUDE_SIMPLE
        #endif
    #endif
#endif

#ifndef LV_ATTRIBUTE_MEM_ALIGN
#define LV_ATTRIBUTE_MEM_ALIGN
#endif
#ifndef LV_ATTRIBUTE_IMG_ANALOGCLOCK_FIRE_GIF_BG
#define LV_ATTRIBUTE_IMG_ANALOGCLOCK_FIRE_GIF_BG
#endif
#ifndef LV_ATTRIBUTE_LARGE_CONST
#define LV_ATTRIBUTE_LARGE_CONST
#endif
const LV_ATTRIBUTE_MEM_ALIGN LV_ATTRIBUTE_LARGE_CONST LV_ATTRIBUTE_IMG_ANALOGCLOCK_FIRE_GIF_BG uint8_t analogclock_fire_gif_bg_map[] = {
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x1c, 0x02, 0x1c, 0x02, 0xf7, 0x77, 0x00,
    0xfa, 0x87, 0x08, 0xf3, 0x7b, 0x06, 0xec, 0x72, 0x06, 0xe5, 0x6a, 0x06, 0xde,
    0x63, 0x06, 0xd7, 0x5b, 0x06, 0xd0, 0x53, 0x06, 0xc9, 0x4b, 0x06, 0xc2, 0x43,
    0x06, 0xbb, 0x3b, 0x06, 0xb4, 0x33, 0x06, 0xad, 0x2b, 0x06, 0xa6, 0x23, 0x06,
    0x9f, 0x1b, 0x06, 0x98, 0x13, 0x06, 0x91, 0x0b, 0x06, 0x8a, 0x03, 0x06, 0x83,
};
"""

# 无 LVGL 宏，但有高密度十六进制字面量（裸字节数组）
HEX_ARRAY_SAMPLE = """// generated image data
const uint8_t image_data[] = {
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x1c, 0x02, 0x1c, 0x02, 0xf7, 0x77, 0x00,
    0xfa, 0x87, 0x08, 0xf3, 0x7b, 0x06, 0xec, 0x72, 0x06, 0xe5, 0x6a, 0x06, 0xde,
    0x63, 0x06, 0xd7, 0x5b, 0x06, 0xd0, 0x53, 0x06, 0xc9, 0x4b, 0x06, 0xc2, 0x43,
    0x06, 0xbb, 0x3b, 0x06, 0xb4, 0x33, 0x06, 0xad, 0x2b, 0x06, 0xa6, 0x23, 0x06,
    0x9f, 0x1b, 0x06, 0x98, 0x13, 0x06, 0x91, 0x0b, 0x06, 0x8a, 0x03, 0x06, 0x83,
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x1c, 0x02, 0x1c, 0x02, 0xf7, 0x77, 0x00,
    0xfa, 0x87, 0x08, 0xf3, 0x7b, 0x06, 0xec, 0x72, 0x06, 0xe5, 0x6a, 0x06, 0xde,
    0x63, 0x06, 0xd7, 0x5b, 0x06, 0xd0, 0x53, 0x06, 0xc9, 0x4b, 0x06, 0xc2, 0x43,
    0x06, 0xbb, 0x3b, 0x06, 0xb4, 0x33, 0x06, 0xad, 0x2b, 0x06, 0xa6, 0x23, 0x06,
    0x9f, 0x1b, 0x06, 0x98, 0x13, 0x06, 0x91, 0x0b, 0x06, 0x8a, 0x03, 0x06, 0x83,
};
"""

# 正常 C 代码（函数、结构体、控制流，十六进制字面量密度低）
NORMAL_C_SAMPLE = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    int x;
    int y;
    char name[32];
} Point;

static int counter = 0;

int add(int a, int b) {
    return a + b;
}

int subtract(int a, int b) {
    return a - b;
}

void print_point(const Point *p) {
    printf("Point(%d, %d, %s)\\n", p->x, p->y, p->name);
}

int main(int argc, char *argv[]) {
    Point p = {1, 2, "origin"};
    int result = add(p.x, p.y);
    int diff = subtract(p.x, p.y);
    if (result > 0) {
        print_point(&p);
    }
    for (int i = 0; i < argc; i++) {
        printf("argv[%d] = %s\\n", i, argv[i]);
    }
    return 0;
}
"""

# 正常 Python 代码
NORMAL_PYTHON_SAMPLE = """import os
import sys
from typing import List, Optional


class Calculator:
    def __init__(self):
        self.history = []

    def add(self, a: int, b: int) -> int:
        result = a + b
        self.history.append(result)
        return result

    def multiply(self, a: int, b: int) -> int:
        result = a * b
        self.history.append(result)
        return result


def main():
    calc = Calculator()
    print(calc.add(1, 2))
    print(calc.multiply(3, 4))


if __name__ == "__main__":
    main()
"""

# 正常 Rust 代码
NORMAL_RUST_SAMPLE = """use std::collections::HashMap;

pub struct Cache {
    data: HashMap<String, String>,
    capacity: usize,
}

impl Cache {
    pub fn new(capacity: usize) -> Self {
        Cache {
            data: HashMap::with_capacity(capacity),
            capacity,
        }
    }

    pub fn get(&self, key: &str) -> Option<&String> {
        self.data.get(key)
    }

    pub fn insert(&mut self, key: String, value: String) {
        if self.data.len() >= self.capacity {
            return;
        }
        self.data.insert(key, value);
    }
}
"""


# ============================================
# _is_resource_file 单元测试
# ============================================

def test_lvgl_resource_file_detected(tmp_path):
    """LVGL 资源文件（含 LV_ATTRIBUTE_IMG_ 宏 + 字节数组）应被检测为资源文件。"""
    f = tmp_path / "analogclock_fire_gif_bg.c"
    f.write_text(LVGL_RESOURCE_SAMPLE, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is True
    assert reason == "lvgl_resource"


def test_lvgl_large_const_marker_detected(tmp_path):
    """LV_ATTRIBUTE_LARGE_CONST 宏也能触发检测。"""
    f = tmp_path / "font_resource.c"
    content = "#ifndef LV_ATTRIBUTE_LARGE_CONST\n#define LV_ATTRIBUTE_LARGE_CONST\n#endif\n"
    f.write_text(content, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is True
    assert reason == "lvgl_resource"


def test_hex_array_density_detected(tmp_path):
    """无 LVGL 宏但十六进制字面量密度高，应检测为资源文件。"""
    f = tmp_path / "image_data.c"
    f.write_text(HEX_ARRAY_SAMPLE, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is True
    assert reason == "hex_array"


def test_normal_c_code_not_detected(tmp_path):
    """正常 C 代码不应被误判为资源文件。"""
    f = tmp_path / "main.c"
    f.write_text(NORMAL_C_SAMPLE, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is False
    assert reason is None


def test_normal_python_code_not_detected(tmp_path):
    """正常 Python 代码不应被误判。"""
    f = tmp_path / "calc.py"
    f.write_text(NORMAL_PYTHON_SAMPLE, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is False
    assert reason is None


def test_normal_rust_code_not_detected(tmp_path):
    """正常 Rust 代码不应被误判。"""
    f = tmp_path / "cache.rs"
    f.write_text(NORMAL_RUST_SAMPLE, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is False
    assert reason is None


def test_empty_file_not_detected(tmp_path):
    """空文件不应被误判。"""
    f = tmp_path / "empty.c"
    f.write_text("", encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is False
    assert reason is None


def test_short_file_not_detected(tmp_path):
    """行数不足 _HEX_MIN_LINES 的短文件不应触发密度检测。"""
    f = tmp_path / "short.c"
    # 只有 5 行，每行高密度十六进制，但行数不足
    content = "0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x1c, 0x02, 0x1c, 0x02,\n" * 5
    f.write_text(content, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is False
    assert reason is None


def test_low_hex_density_not_detected(tmp_path):
    """十六进制字面量密度低于阈值的不应触发检测。"""
    f = tmp_path / "normal.c"
    # 正常 C 代码：每行最多 1-2 个十六进制字面量
    content = (
        "#include <stdio.h>\n"
        "int main() {\n"
        "    int flag = 0xFF;\n"
        "    int mask = 0xAB;\n"
        "    printf(\"%d\\n\", flag);\n"
        "    return 0;\n"
        "}\n"
    )
    f.write_text(content, encoding="utf-8")

    is_res, reason = _is_resource_file(str(f))

    assert is_res is False
    assert reason is None


def test_nonexistent_file_returns_false():
    """文件不存在时返回 (False, None)，不抛异常。"""
    is_res, reason = _is_resource_file("/nonexistent/path/file.c")

    assert is_res is False
    assert reason is None


# ============================================
# 常量验证
# ============================================

def test_lvgl_markers_contain_img_and_large_const():
    """_LVGL_RESOURCE_MARKERS 应包含 LV_ATTRIBUTE_IMG_ 和 LV_ATTRIBUTE_LARGE_CONST。"""
    assert "LV_ATTRIBUTE_IMG_" in _LVGL_RESOURCE_MARKERS
    assert "LV_ATTRIBUTE_LARGE_CONST" in _LVGL_RESOURCE_MARKERS


def test_hex_density_threshold_is_8():
    """十六进制密度阈值应为 8（平均每行 > 8 个十六进制字面量）。"""
    assert _HEX_DENSITY_THRESHOLD == 8


def test_hex_min_lines_is_10():
    """最少行数要求应为 10。"""
    assert _HEX_MIN_LINES == 10


def test_hex_literal_regex():
    """_HEX_LITERAL_RE 正确匹配 0x00-0xFF 字面量。"""
    matches = _HEX_LITERAL_RE.findall("0x47, 0x49, 0x46, 0x38")
    assert len(matches) == 4
    assert "0x47" in matches

    # 不匹配非十六进制
    no_match = _HEX_LITERAL_RE.findall("hello world")
    assert len(no_match) == 0


# ============================================
# _parse_file_worker 集成测试
# ============================================

def test_parse_file_worker_skips_lvgl_resource(tmp_path):
    """_parse_file_worker 对 LVGL 资源文件返回 skip_resource。"""
    f = tmp_path / "analogclock_fire_gif_bg.c"
    f.write_text(LVGL_RESOURCE_SAMPLE, encoding="utf-8")

    args = (str(f), str(f), "c", "test_module", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "skip_resource"
    assert payload == "lvgl_resource"


def test_parse_file_worker_skips_hex_array(tmp_path):
    """_parse_file_worker 对高密度十六进制数组返回 skip_resource。"""
    f = tmp_path / "image_data.c"
    f.write_text(HEX_ARRAY_SAMPLE, encoding="utf-8")

    args = (str(f), str(f), "c", "test_module", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "skip_resource"
    assert payload == "hex_array"


def test_parse_file_worker_parses_normal_c(tmp_path):
    """_parse_file_worker 对正常 C 文件正常解析（不跳过）。"""
    f = tmp_path / "main.c"
    f.write_text(NORMAL_C_SAMPLE, encoding="utf-8")

    args = (str(f), str(f), "c", "test_module", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "ok"
    assert payload is not None
    assert payload["language"] == "c"


def test_is_resource_file_is_module_level():
    """_is_resource_file 应是模块级函数（可 pickle，多进程要求）。"""
    import pickle
    pickle.dumps(_is_resource_file)


def test_no_size_based_check_in_worker():
    """_parse_file_worker 不应再使用基于文件大小的判断（os.path.getsize 跳过）。

    用户要求：判断内容格式而非大小。
    验证：worker 函数源码中不应有 '> 1 * 1024 * 1024' 大小判断。
    """
    import inspect
    import callwarden.db.db_build as db_build_mod

    src = inspect.getsource(db_build_mod._parse_file_worker)
    # 不应再有大文件大小判断
    assert "1 * 1024 * 1024" not in src
    assert "skip_large" not in src
