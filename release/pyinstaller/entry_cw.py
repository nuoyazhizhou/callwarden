#!/usr/bin/env python3
"""PyInstaller 入口包装：cw 命令。

PyInstaller 不支持 module:function 形式的入口，需要包装脚本调用 main()。
"""
from callwarden.cw import main

if __name__ == "__main__":
    main()
