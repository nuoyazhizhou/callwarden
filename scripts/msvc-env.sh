#!/usr/bin/env bash
# =============================================================================
# msvc-env.sh — 在 Git Bash 沙箱中修复 MSVC 链接器遮蔽与 LIB 环境
#
# 问题背景：
#   Git Bash 的 PATH 中 PortableGit/usr/bin/link.exe（GNU coreutils）会遮蔽
#   MSVC 的 link.exe，导致 cargo/rustc 链接时报
#   "link: extra operand" / "missing operand after '\377\376'"。
#   且缺少 vcvars 设置的 LIB 环境变量，会报 LNK1181: cannot open kernel32.lib。
#
# 用法（在 Git Bash 中）：
#   source scripts/msvc-env.sh          # 注入 MSVC bin 到 PATH 前 + 设置 LIB/INCLUDE
#   然后正常跑 cargo check / cargo test
#
# 自动探测，不写死版本号；找不到则提示运行 vcvars64.bat。
# =============================================================================

# --- 1. 探测 VS 安装与 MSVC 工具集版本 ---
VSROOT=""
for base in "/c/Program Files/Microsoft Visual Studio" "/c/Program Files (x86)/Microsoft Visual Studio"; do
  if [ -d "$base" ]; then VSROOT="$base"; break; fi
done
if [ -z "$VSROOT" ]; then
  echo "[msvc-env] ERROR: 未找到 Visual Studio 安装目录" >&2
  return 1 2>/dev/null || exit 1
fi

# 找最新版（2022/2019/2017 目录取版本号最大者），VS 结构为 <ver>/<Edition>/VC
VSED=""
for d in "$VSROOT"/20*/; do
  [ -d "$d" ] || continue
  # 该版本下找含 VC 工具集的 Edition（Community/Professional/Enterprise/BuildTools）
  for ed in "$d"*/; do
    [ -d "$ed/VC/Auxiliary/Build" ] && VSED="$ed"
  done
done
if [ -z "$VSED" ]; then
  echo "[msvc-env] ERROR: 未找到含 VC 工具集的 VS 版本目录" >&2
  return 1 2>/dev/null || exit 1
fi
echo "[msvc-env] VS Edition = $VSED"

# MSVC 工具集版本（VC/Tools/MSVC/<ver>）
MSVC_VER=""
for d in "$VSED"VC/Tools/MSVC/*/; do
  [ -d "$d" ] && MSVC_VER=$(basename "$d")
done
if [ -z "$MSVC_VER" ]; then
  echo "[msvc-env] ERROR: 未找到 VC/Tools/MSVC/<ver>" >&2
  return 1 2>/dev/null || exit 1
fi

MSVC_TOOLS="$VSED"VC/Tools/MSVC/$MSVC_VER
MSVC_BIN="$MSVC_TOOLS/bin/Hostx64/x64"
MSVC_LIB="$MSVC_TOOLS/lib/x64"

# --- 2. 探测 Windows SDK 版本 ---
SDKROOT=""
for base in "/c/Program Files (x86)/Windows Kits/10/Lib" "/c/Program Files/Windows Kits/10/Lib"; do
  if [ -d "$base" ]; then SDKROOT="$base"; break; fi
done
if [ -z "$SDKROOT" ]; then
  echo "[msvc-env] ERROR: 未找到 Windows Kits/10/Lib" >&2
  return 1 2>/dev/null || exit 1
fi
SDK_VER=""
for d in "$SDKROOT"/*/; do
  [ -d "$d" ] && SDK_VER=$(basename "$d")
done
if [ -z "$SDK_VER" ]; then
  echo "[msvc-env] ERROR: 未找到 SDK 版本目录" >&2
  return 1 2>/dev/null || exit 1
fi
SDK_LIB="$SDKROOT/$SDK_VER"

# --- 3. 注入环境 ---
# PATH 前置 MSVC bin，确保 link.exe/cl.exe 解析到 MSVC 而非 Git 的 GNU 工具
export PATH="$MSVC_BIN:$PATH"

# 转成 Windows 格式路径（MSVC link.exe 是 Windows 程序，不认 /c/... 或 /c/Program Files）
winpath() { cygpath -w "$1" 2>/dev/null || echo "$1" | sed 's|^/\([a-zA-Z]\)/|\1:/|; s|/|\\|g'; }
MSVC_LIB_WIN=$(winpath "$MSVC_LIB")
SDK_UM_WIN=$(winpath "$SDK_LIB/um/x64")
SDK_UCRT_WIN=$(winpath "$SDK_LIB/ucrt/x64")

# LIB：MSVC 运行库 + SDK um（系统 API）+ SDK ucrt（C 运行库）——分号分隔的 Windows 路径
export LIB="$MSVC_LIB_WIN;$SDK_UM_WIN;$SDK_UCRT_WIN"
# INCLUDE：编译期需要（cl.exe / 部分构建脚本）
export INCLUDE="$(winpath "$MSVC_TOOLS/include");$(winpath "$SDK_LIB/um");$(winpath "$SDK_LIB/ucrt");$(winpath "$SDK_LIB/shared")"

echo "[msvc-env] MSVC=$MSVC_TOOLS"
echo "[msvc-env] SDK =$SDK_LIB"
echo "[msvc-env] link => $(which link.exe)"
echo "[msvc-env] LIB  = $LIB"
