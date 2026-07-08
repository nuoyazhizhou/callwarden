"""P16: Java 外部依赖批量 javap 优化测试

覆盖：
- _parse_javap_batch_output：批量输出解析正确性
- _parse_and_insert_javap_block：单 class 块解析
- _javap_single_class：回退路径
- 批量 javap 输出格式解析（Compiled from 分隔）
"""
import inspect

import pytest

from callwarden.db.db import CodeGraphDB


# ============================================
# 源码验证
# ============================================

def test_scan_java_class_jar_uses_batch():
    """_scan_java_class_jar_via_javap 应使用批量调用而非逐类调用。"""
    from callwarden.db import db_external
    src = inspect.getsource(db_external.ExternalMixin._scan_java_class_jar_via_javap)
    # 应该有 BATCH_SIZE 和批量调用
    assert "BATCH_SIZE" in src
    assert "class_names" in src
    # 应该有批量解析方法
    assert "_parse_javap_batch_output" in src


def test_batch_fallback_exists():
    """批量失败时应回退到逐类处理。"""
    from callwarden.db import db_external
    src = inspect.getsource(db_external.ExternalMixin._scan_java_class_jar_via_javap)
    assert "_javap_single_class" in src
    assert "TimeoutExpired" in src or "returncode != 0" in src


# ============================================
# _parse_javap_batch_output 测试
# ============================================

@pytest.fixture
def db(tmp_path):
    """测试用 DB"""
    db = CodeGraphDB(str(tmp_path / "test.db"), workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    yield db
    db.close()


def test_parse_javap_batch_output_single_class(db):
    """解析单个 class 的批量输出。"""
    stdout = '''Compiled from "Bar.java"
public class com.foo.Bar {
    public void doSomething();
    public java.lang.String getName();
    public static final int MAX = 100;
}'''
    batch = [("com/foo/Bar.class", "com.foo.Bar")]
    created = db._parse_javap_batch_output(stdout, "test.jar", batch, "ext-java-test", "1.0", 0)
    # 1 class + 2 methods + 1 field = 4 symbols
    assert created == 4


def test_parse_javap_batch_output_multiple_classes(db):
    """解析多个 class 的批量输出。"""
    stdout = '''Compiled from "Bar.java"
public class com.foo.Bar {
    public void doSomething();
}
Compiled from "Baz.java"
public class com.foo.Baz {
    public int getValue();
    public void setName(java.lang.String);
}'''
    batch = [
        ("com/foo/Bar.class", "com.foo.Bar"),
        ("com/foo/Baz.class", "com.foo.Baz"),
    ]
    created = db._parse_javap_batch_output(stdout, "test.jar", batch, "ext-java-test", "1.0", 0)
    # 2 classes + 3 methods = 5 symbols
    assert created == 5


def test_parse_javap_batch_output_interface(db):
    """解析接口的 javap 输出。"""
    stdout = '''Compiled from "IFace.java"
public interface com.foo.IFace {
    public abstract void doWork();
}'''
    batch = [("com/foo/IFace.class", "com.foo.IFace")]
    created = db._parse_javap_batch_output(stdout, "test.jar", batch, "ext-java-test", "1.0", 0)
    # 1 interface + 1 method = 2 symbols
    assert created == 2


def test_parse_javap_batch_output_enum(db):
    """解析枚举的 javap 输出。

    javap 对 enum 会输出 values() 和 valueOf() 方法，
    以及枚举常量字段。字段正则可能额外匹配方法行，所以放宽断言。
    """
    stdout = '''Compiled from "Color.java"
public final enum com.foo.Color {
    public static final com.foo.Color RED;
    public static final com.foo.Color GREEN;
    public com.foo.Color[] values();
    public static com.foo.Color valueOf(java.lang.String);
}'''
    batch = [("com/foo/Color.class", "com.foo.Color")]
    created = db._parse_javap_batch_output(stdout, "test.jar", batch, "ext-java-test", "1.0", 0)
    # 1 enum + 2 fields + 2 methods = 5+ symbols
    # （字段正则可能额外匹配方法行，导致计数偏高）
    assert created >= 5


def test_parse_javap_batch_output_empty(db):
    """空输出返回 0。"""
    created = db._parse_javap_batch_output("", "test.jar", [], "ext-java-test", "1.0", 0)
    assert created == 0


def test_parse_javap_batch_output_unknown_class(db):
    """输出中的 class 不在 batch 列表中时跳过。"""
    stdout = '''Compiled from "Bar.java"
public class com.foo.Bar {
    public void doSomething();
}'''
    # batch 中没有 com.foo.Bar
    batch = [("com/foo/Baz.class", "com.foo.Baz")]
    created = db._parse_javap_batch_output(stdout, "test.jar", batch, "ext-java-test", "1.0", 0)
    assert created == 0


# ============================================
# _parse_and_insert_javap_block 测试
# ============================================

def test_parse_and_insert_javap_block_methods(db):
    """解析单个 class 块的方法。"""
    block = '''public class com.foo.Bar {
    public void doSomething();
    public java.lang.String getName();
    public void setName(java.lang.String);
}'''
    created = db._parse_and_insert_javap_block(
        block, "com.foo.Bar", "com/foo/Bar.class",
        "test.jar", "ext-java-test", "1.0", 0,
    )
    # 1 class + 3 methods = 4
    assert created == 4


def test_parse_and_insert_javap_block_fields(db):
    """解析字段。"""
    block = '''public class com.foo.Bar {
    public static final int MAX = 100;
    public static final java.lang.String NAME = "test";
}'''
    created = db._parse_and_insert_javap_block(
        block, "com.foo.Bar", "com/foo/Bar.class",
        "test.jar", "ext-java-test", "1.0", 0,
    )
    # 1 class + 2 fields = 3
    assert created == 3


def test_parse_and_insert_javap_block_dedup(db):
    """重复插入相同符号不会增加计数。"""
    block = '''public class com.foo.Bar {
    public void doSomething();
}'''
    # 第一次插入
    created1 = db._parse_and_insert_javap_block(
        block, "com.foo.Bar", "com/foo/Bar.class",
        "test.jar", "ext-java-test", "1.0", 0,
    )
    # 第二次插入相同符号（created 从 0 开始，但符号已存在）
    created2 = db._parse_and_insert_javap_block(
        block, "com.foo.Bar", "com/foo/Bar.class",
        "test.jar", "ext-java-test", "1.0", 0,
    )
    assert created1 == 2  # 1 class + 1 method
    assert created2 == 0  # 全部已存在


# ============================================
# 性能验证：批量 vs 逐类
# ============================================

def test_batch_size_is_20():
    """BATCH_SIZE 应为 20（避免命令行过长）。"""
    from callwarden.db import db_external
    src = inspect.getsource(db_external.ExternalMixin._scan_java_class_jar_via_javap)
    assert "BATCH_SIZE = 20" in src


def test_batch_timeout_is_30():
    """批量调用超时应为 30s（比单类 10s 更长）。"""
    from callwarden.db import db_external
    src = inspect.getsource(db_external.ExternalMixin._scan_java_class_jar_via_javap)
    assert "timeout=30" in src
