"""种子样本(Python):供收敛套件 T1 种子 workspace build_graph 用。

刻意保持小而多样:模块级函数、类、方法、跨函数调用,足以让
build_graph 产出可查询的符号与调用边(callers/callees/call-chain/stats)。
"""


def add(a, b):
    """两数相加。"""
    return a + b


def multiply(a, b):
    """两数相乘(通过重复 add 实现,制造调用边)。"""
    result = 0
    for _ in range(b):
        result = add(result, a)
    return result


class Calculator:
    """一个最小计算器类,含被 compute 调用的方法。"""

    def __init__(self, base=0):
        self.base = base

    def apply(self, x):
        """在 base 上叠加,调用模块级 add。"""
        return add(self.base, x)

    def scale(self, x, factor):
        """缩放,调用模块级 multiply。"""
        return multiply(self.apply(x), factor)


def compute(values):
    """入口函数:实例化 Calculator 并聚合,制造多层调用链。"""
    calc = Calculator(base=10)
    total = 0
    for v in values:
        total = add(total, calc.scale(v, 2))
    return total
