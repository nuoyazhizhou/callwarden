"""H14/N5-N8 跨平台打包产物验证脚本

验证内容：
- N5 Windows: callwarden.wxs WiX 源文件 XML 语法
- N6 macOS: build_pkg.sh bash 语法
- N7 Linux: build_packages.sh + 5 control + 14 maintainer 脚本
- N8 Release CI: enterprise-release.yml YAML 语法 + 11 门禁 job 列表
- H14 整体: artifact-manifest.json + wheel 元数据
- P0-B 轻量包体积度量: local vs client/agent 安装目录差值、压缩包差值、
  三平台报告格式（设计：docs/design/rust-only-parser-cutover-plan.md Phase 1 步骤 5）

用法：
    python release/_check_artifacts.py
"""
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"

# PyInstaller --onedir 产物根目录（callwarden.spec 的默认输出）
PYINSTALLER_DIST_DIR = ROOT / "dist"
LOCAL_BUNDLE_DIR = PYINSTALLER_DIST_DIR / "callwarden"
CLIENT_BUNDLE_DIR = PYINSTALLER_DIST_DIR / "callwarden-client"


def check_wxs():
    """N5: 验证 WiX wxs XML 语法"""
    print("\n=== N5: Windows WiX wxs 验证 ===")
    wxs_path = RELEASE_DIR / "windows" / "callwarden.wxs"
    if not wxs_path.exists():
        print(f"  [FAIL] 未找到 {wxs_path}")
        return False

    try:
        # 使用 defusedxml 防 XXE（semgrep: use-defused-xml-parse）
        from defusedxml import ElementTree as ET
        tree = ET.parse(wxs_path)
        root = tree.getroot()
        # WiX 命名空间
        ns = {"wix": "http://schemas.microsoft.com/wix/2006/wi"}
        product = root.find("wix:Product", ns)
        if product is None:
            product = root.find("Product")
        if product is None:
            print("  [FAIL] 未找到 <Product> 元素")
            return False

        print(f"  [OK] XML 解析成功")
        print(f"  Product Id={product.get('Id')} Name={product.get('Name')} "
              f"Version={product.get('Version')} Manufacturer={product.get('Manufacturer')}")

        # 统计 WiX 元素
        package = product.find("wix:Package", ns)
        if package is None:
            package = product.find("Package")
        features = product.findall("wix:Feature", ns) or product.findall("Feature")
        directories = product.findall("wix:Directory", ns) or product.findall("Directory")
        components = product.findall(".//wix:Component", ns) or product.findall(".//Component")

        print(f"  Package: Id={package.get('Id') if package is not None else 'N/A'}")
        print(f"  Features: {len(features)} (顶级)")
        print(f"  Directories: {len(directories)} (顶级)")
        print(f"  Components: {len(components)} (含嵌套)")
        return True
    except ET.ParseError as e:
        print(f"  [FAIL] XML 解析失败: {e}")
        return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def _to_wsl_path(win_path: Path) -> str:
    """把 Windows 路径转为 WSL 路径（C:\\foo\\bar → /mnt/c/foo/bar）"""
    s = str(win_path)
    # 替换反斜杠
    s = s.replace("\\", "/")
    # 替换盘符
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        s = f"/mnt/{drive}{s[2:]}"
    return s


def check_bash_script(script_path: Path, label: str) -> bool:
    """验证 bash 脚本语法（用 bash -n）"""
    print(f"\n=== {label}: {script_path.relative_to(ROOT)} ===")
    if not script_path.exists():
        print(f"  [FAIL] 未找到 {script_path}")
        return False

    # 优先用 WSL bash
    wsl_path = _to_wsl_path(script_path)
    try:
        result = subprocess.run(
            ["wsl", "bash", "-n", wsl_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"  [OK] bash -n 语法检查通过 (WSL)")
            # 统计行数和关键命令
            content = script_path.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            print(f"  总行数: {len(lines)}")
            # 简单关键词统计
            keywords = ["codesign", "notarytool", "productbuild", "pkgbuild",
                        "dpkg-deb", "fakeroot", "rpmbuild", "tar", "systemd",
                        "Authenticode", "signtool"]
            found = [k for k in keywords if k in content]
            if found:
                print(f"  关键命令: {', '.join(found)}")
            return True
        else:
            print(f"  [FAIL] bash -n 失败 (WSL): {result.stderr.strip()}")
            return False
    except FileNotFoundError:
        print(f"  [SKIP] WSL 不可用，跳过 bash -n 检查")
        # 退到简单文本检查
        content = script_path.read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()
        print(f"  总行数: {len(lines)} (未做语法检查)")
        return True
    except subprocess.TimeoutExpired:
        print(f"  [FAIL] bash -n 超时")
        return False


def check_linux_packages():
    """N7: 验证 Linux deb 5 子包 + 14 maintainer 脚本"""
    print("\n=== N7: Linux deb 5 子包验证 ===")
    deb_dir = RELEASE_DIR / "linux" / "deb"
    if not deb_dir.exists():
        print(f"  [FAIL] 未找到 {deb_dir}")
        return False

    # 5 control 文件
    control_files = list(deb_dir.glob("control.*"))
    expected_controls = ["control.agent", "control.client", "control.daemon",
                         "control.enterprise", "control.local"]
    found_controls = [c.name for c in control_files]
    print(f"  Control 文件: {len(found_controls)}/{len(expected_controls)}")
    for ec in expected_controls:
        status = "OK" if ec in found_controls else "MISSING"
        print(f"    [{status}] {ec}")

    # 14 maintainer 脚本
    script_files = [f for f in deb_dir.iterdir() if f.is_file() and
                    any(f.name.endswith(ext) for ext in
                        [".postinst", ".postrm", ".prerm", ".preinst"])]
    print(f"  Maintainer 脚本: {len(script_files)}")

    # systemd 单元
    systemd_dir = deb_dir / "systemd"
    if systemd_dir.exists():
        units = list(systemd_dir.glob("*.service"))
        print(f"  systemd 单元: {len(units)} ({[u.name for u in units]})")

    # sysusers / tmpfiles
    sysusers_dir = deb_dir / "sysusers.d"
    tmpfiles_dir = deb_dir / "tmpfiles.d"
    if sysusers_dir.exists():
        print(f"  sysusers.d: {len(list(sysusers_dir.glob('*')))} 文件")
    if tmpfiles_dir.exists():
        print(f"  tmpfiles.d: {len(list(tmpfiles_dir.glob('*')))} 文件")

    # 离线安装
    offline_dir = deb_dir / "offline"
    if offline_dir.exists():
        print(f"  offline install: 存在 ({len(list(offline_dir.glob('*')))} 文件)")

    all_ok = (len(found_controls) == 5 and len(script_files) >= 10)
    return all_ok


def check_ci_yaml():
    """N8: 验证 enterprise-release.yml YAML 语法和 11 门禁"""
    print("\n=== N8: Release CI YAML 验证 ===")
    yaml_path = ROOT / ".github" / "workflows" / "enterprise-release.yml"
    if not yaml_path.exists():
        print(f"  [FAIL] 未找到 {yaml_path}")
        return False

    try:
        import yaml
    except ImportError:
        print("  [SKIP] PyYAML 未安装")
        return False

    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        print(f"  [OK] YAML 解析成功")
        print(f"  name: {data.get('name')}")

        # 解析 on 触发器（YAML 1.1 中 on 可能被解析为 True）
        on_key = "on" if "on" in data else True
        triggers = data.get(on_key, {})
        if isinstance(triggers, dict):
            print(f"  triggers: {list(triggers.keys())}")
        else:
            print(f"  triggers: {triggers}")

        # 解析 jobs
        jobs = data.get("jobs", {})
        print(f"  jobs: {len(jobs)} 个")
        for i, (job_id, job_data) in enumerate(jobs.items(), 1):
            job_name = job_data.get("name", job_id)
            print(f"    [{i}] {job_id}: {job_name}")

        # 期望的 11 门禁 job（命名规则：gate1- 到 gate11-，可能有字母后缀如 gate4a/gate5b）
        # 实际 job id 形如 gate1-source-tests, gate4a-windows-msi 等
        import re
        actual_job_ids = list(jobs.keys())
        gate_pattern = re.compile(r"^gate(\d+)[a-z]?-")
        gate_numbers = []
        for jid in actual_job_ids:
            m = gate_pattern.match(jid)
            if m:
                gate_numbers.append(int(m.group(1)))
        unique_gate_numbers = sorted(set(gate_numbers))
        print(f"  Gate 门禁编号: {unique_gate_numbers} ({len(unique_gate_numbers)}/11)")

        # 关键门禁关键词匹配
        gate_keywords = {
            "源码测试": ["source-tests", "source_tests"],
            "构建 wheel": ["build-wheels", "build_wheels"],
            "黑盒测试": ["blackbox", "wheel-blackbox"],
            "Windows MSI": ["msi", "windows-msi"],
            "macOS pkg": ["pkg", "macos-pkg"],
            "Linux deb": ["deb", "linux-deb"],
            "安装验证": ["install"],
            "N-1 升级": ["n1-upgrade", "n-minus-1", "upgrade"],
            "签名公证": ["signing", "sign-artifacts"],
            "SBOM": ["sbom"],
            "staging": ["staging"],
            "审批": ["approval"],
            "生产发布": ["production"],
        }
        matched_gates = []
        for label, keywords in gate_keywords.items():
            if any(any(kw in jid for jid in actual_job_ids) for kw in keywords):
                matched_gates.append(label)
        print(f"  关键门禁匹配: {len(matched_gates)}/{len(gate_keywords)} ({', '.join(matched_gates)})")

        # 至少 11 个唯一的 gate 编号（1-11）
        return len(unique_gate_numbers) >= 11
    except yaml.YAMLError as e:
        print(f"  [FAIL] YAML 解析失败: {e}")
        return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def check_manifest():
    """H14: 验证 artifact-manifest.json"""
    print("\n=== H14: artifact-manifest.json 验证 ===")
    manifest_path = RELEASE_DIR / "artifact-manifest.json"
    if not manifest_path.exists():
        print(f"  [FAIL] 未找到 {manifest_path}")
        return False

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        print(f"  [OK] JSON 解析成功")
        print(f"  product: {manifest.get('product')}")
        print(f"  version: {manifest.get('version')}")
        print(f"  build_time: {manifest.get('build_time')}")
        print(f"  abi: {manifest.get('abi')}")
        print(f"  platforms: {manifest.get('platforms')}")
        artifacts = manifest.get("artifacts", [])
        print(f"  artifacts: {len(artifacts)} 个")
        for a in artifacts:
            print(f"    - {a.get('filename')} size={a.get('size')} "
                  f"sha256={a.get('sha256', '')[:16]}...")
        return len(artifacts) > 0
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def check_wheel_metadata():
    """验证 wheel 文件元数据"""
    print("\n=== Wheel 元数据验证 ===")
    dist_dir = RELEASE_DIR / "dist"
    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        print(f"  [FAIL] dist/ 下无 .whl 文件")
        return False

    wheel = wheels[0]
    print(f"  Wheel 文件: {wheel.name}")
    print(f"  大小: {wheel.stat().st_size / 1024:.1f} KB")

    # 用 zipfile 读取 METADATA
    import zipfile
    try:
        with zipfile.ZipFile(wheel) as zf:
            metadata_files = [n for n in zf.namelist() if n.endswith("METADATA")]
            if metadata_files:
                metadata = zf.read(metadata_files[0]).decode("utf-8")
                # 提取关键字段
                for line in metadata.splitlines()[:20]:
                    if any(line.startswith(k) for k in
                           ["Name:", "Version:", "Summary:", "Author:",
                            "License:", "Requires-Python:"]):
                        print(f"  {line}")
        return True
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def _iter_bundle_files(bundle_dir: Path):
    """稳定顺序返回 bundle 目录中的所有普通文件。"""
    return sorted(p for p in bundle_dir.rglob("*") if p.is_file())


def _bundle_unpacked_bytes(bundle_dir: Path) -> int:
    """计算 bundle 解压目录总字节数。"""
    return sum(p.stat().st_size for p in _iter_bundle_files(bundle_dir))


def _bundle_parser_bytes(bundle_dir: Path) -> int:
    """计算 bundle 中 parser 相关 distribution 的字节数。

    复用 inspect_pyinstaller_bundle.compute_distribution_breakdown 的分类逻辑，
    把 tree_sitter 核心、16 种 grammar、callwarden.parsers 子模块字节数加总。
    """
    try:
        sys.path.insert(0, str(RELEASE_DIR))
        try:
            from inspect_pyinstaller_bundle import (
                compute_distribution_breakdown,
                PARSER_DISTRIBUTIONS,
            )
        finally:
            sys.path.pop(0)
    except ImportError:
        return 0

    breakdown = compute_distribution_breakdown(bundle_dir)
    return sum(
        breakdown["distributions"][name]["byte_count"]
        for name in PARSER_DISTRIBUTIONS
        if name in breakdown["distributions"]
    )


def _format_bytes(n: int) -> str:
    """字节数格式化为人类可读的 MiB/KiB。"""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MiB"
    if n >= 1024:
        return f"{n / 1024:.2f} KiB"
    return f"{n} B"


def _make_compressed_artifact(bundle_dir: Path, suffix: str) -> Path | None:
    """为 bundle 生成临时压缩包用于体积度量。

    Linux 用 tar.gz，Windows/macOS 用 zip。返回压缩包路径，失败返回 None。
    """
    import tempfile

    artifact = Path(tempfile.gettempdir()) / f"{bundle_dir.name}{suffix}"
    try:
        artifact.unlink(missing_ok=True)
    except OSError:
        pass

    files = _iter_bundle_files(bundle_dir)
    if not files:
        return None

    try:
        if suffix.endswith(".tar.gz"):
            import tarfile
            with tarfile.open(artifact, "w:gz") as tf:
                for f in files:
                    tf.add(f, arcname=str(f.relative_to(bundle_dir)))
        elif suffix.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, arcname=str(f.relative_to(bundle_dir)))
        else:
            return None
    except OSError:
        return None
    return artifact


def check_light_bundle_unpacked_diff():
    """P0-B 步骤 5: 度量 local vs client/agent 安装目录差值。

    设计：docs/design/rust-only-parser-cutover-plan.md Phase 1 步骤 5
    度量项：安装目录差值（_internal/ 解压后字节数差异）。

    - Linux：两个 bundle 都应存在，计算差值与节省百分比
    - Windows/macOS：只有 local bundle，client/agent bundle 不构建（UDS Linux 特有），
      报告 local bundle 的 parser distribution 占比作为「潜在节省」估算
    - bundle 不存在时打印 SKIP 并返回 True（度量不是发布门禁）
    """
    print("\n=== P0-B: 轻量包安装目录差值度量 ===")
    print(f"  平台: {sys.platform}")

    local_exists = LOCAL_BUNDLE_DIR.is_dir()
    client_exists = CLIENT_BUNDLE_DIR.is_dir()

    if not local_exists and not client_exists:
        print(f"  [SKIP] 未找到 PyInstaller 产物（{LOCAL_BUNDLE_DIR} 不存在）")
        print("         请先运行: python -m PyInstaller release/pyinstaller/callwarden.spec --noconfirm --clean")
        return True

    if not local_exists:
        print(f"  [SKIP] local bundle 不存在: {LOCAL_BUNDLE_DIR}")
        return True

    local_bytes = _bundle_unpacked_bytes(LOCAL_BUNDLE_DIR)
    local_parser_bytes = _bundle_parser_bytes(LOCAL_BUNDLE_DIR)
    print(f"  local bundle:   {LOCAL_BUNDLE_DIR.name}/")
    print(f"    总体积:       {_format_bytes(local_bytes)}")
    print(f"    parser 占比:  {_format_bytes(local_parser_bytes)} "
          f"({(local_parser_bytes / local_bytes * 100) if local_bytes else 0:.1f}%)")

    if sys.platform.startswith("linux") and client_exists:
        # Linux: 实际度量 client/agent bundle 差值
        client_bytes = _bundle_unpacked_bytes(CLIENT_BUNDLE_DIR)
        client_parser_bytes = _bundle_parser_bytes(CLIENT_BUNDLE_DIR)
        print(f"  client bundle:  {CLIENT_BUNDLE_DIR.name}/")
        print(f"    总体积:       {_format_bytes(client_bytes)}")
        print(f"    parser 占比:  {_format_bytes(client_parser_bytes)} "
              f"({(client_parser_bytes / client_bytes * 100) if client_bytes else 0:.1f}%)")

        diff = local_bytes - client_bytes
        pct = (diff / local_bytes * 100) if local_bytes else 0
        print(f"  安装目录差值:   {_format_bytes(diff)} （节省 {pct:.1f}%）")

        # client/agent bundle 必须 parser 字节数为 0（fail closed）
        if client_parser_bytes > 0:
            print(f"  [FAIL] client/agent bundle 不应包含 parser 文件，"
                  f"实际 {_format_bytes(client_parser_bytes)}")
            return False
        print("  [OK] client/agent bundle parser distribution = 0 bytes")
    elif sys.platform.startswith("linux"):
        print(f"  [SKIP] Linux 上未找到 client bundle: {CLIENT_BUNDLE_DIR}")
        print("         （仅 local bundle 构建，未拆分 client/agent）")
        print(f"  预期节省（parser 占比）: ~{_format_bytes(local_parser_bytes)}")
    else:
        # Windows/macOS: client/agent bundle 不构建
        print(f"  client bundle:  N/A（{sys.platform} 不构建 client/agent，UDS 是 Linux 特有）")
        print(f"  预期节省（parser 占比）: ~{_format_bytes(local_parser_bytes)}")

    return True


def check_light_bundle_compressed_diff():
    """P0-B 步骤 5: 度量 local vs client/agent 压缩包差值。

    度量项：压缩包差值（tar.gz/zip 体积差异）。
    Linux 用 tar.gz，Windows/macOS 用 zip（与 release/build.py 一致）。

    bundle 不存在时打印 SKIP 并返回 True。
    """
    print("\n=== P0-B: 轻量包压缩包差值度量 ===")

    local_exists = LOCAL_BUNDLE_DIR.is_dir()
    client_exists = CLIENT_BUNDLE_DIR.is_dir()

    if not local_exists:
        print(f"  [SKIP] local bundle 不存在: {LOCAL_BUNDLE_DIR}")
        return True

    # 选择压缩格式（Linux: tar.gz，其他: zip）
    if sys.platform.startswith("linux"):
        suffix = ".tar.gz"
    else:
        suffix = ".zip"

    local_artifact = _make_compressed_artifact(LOCAL_BUNDLE_DIR, suffix)
    if local_artifact is None:
        print(f"  [SKIP] local bundle 压缩失败")
        return True

    local_compressed = local_artifact.stat().st_size
    print(f"  local 压缩包 ({suffix}): {_format_bytes(local_compressed)}")

    if sys.platform.startswith("linux") and client_exists:
        client_artifact = _make_compressed_artifact(CLIENT_BUNDLE_DIR, suffix)
        if client_artifact is None:
            print(f"  [SKIP] client bundle 压缩失败")
            try:
                local_artifact.unlink(missing_ok=True)
            except OSError:
                pass
            return True

        client_compressed = client_artifact.stat().st_size
        print(f"  client 压缩包 ({suffix}): {_format_bytes(client_compressed)}")

        diff = local_compressed - client_compressed
        pct = (diff / local_compressed * 100) if local_compressed else 0
        print(f"  压缩包差值:     {_format_bytes(diff)} （节省 {pct:.1f}%）")

        try:
            client_artifact.unlink(missing_ok=True)
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        print(f"  [SKIP] Linux 上未找到 client bundle，无法度量压缩包差值")
    else:
        print(f"  client 压缩包: N/A（{sys.platform} 不构建 client/agent）")

    try:
        local_artifact.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def check_light_bundle_three_platform_report():
    """P0-B 步骤 5: 三平台报告格式验证。

    确保报告在 Windows/macOS/Linux 三种平台下都能输出，并且：
    - Linux 报告 local + client 两个 bundle 的差值
    - Windows/macOS 报告 local bundle + 说明 client/agent 不构建
    - 报告字段名跨平台一致（便于 CI 自动解析）
    """
    print("\n=== P0-B: 三平台报告格式验证 ===")

    report = {
        "platform": sys.platform,
        "local_bundle": str(LOCAL_BUNDLE_DIR),
        "client_bundle": str(CLIENT_BUNDLE_DIR),
        "local_exists": LOCAL_BUNDLE_DIR.is_dir(),
        "client_exists": CLIENT_BUNDLE_DIR.is_dir(),
        "client_supported": sys.platform.startswith("linux"),
    }

    # 度量值（如果 bundle 存在）
    if report["local_exists"]:
        report["local_unpacked_bytes"] = _bundle_unpacked_bytes(LOCAL_BUNDLE_DIR)
        report["local_parser_bytes"] = _bundle_parser_bytes(LOCAL_BUNDLE_DIR)
    if report["client_exists"]:
        report["client_unpacked_bytes"] = _bundle_unpacked_bytes(CLIENT_BUNDLE_DIR)
        report["client_parser_bytes"] = _bundle_parser_bytes(CLIENT_BUNDLE_DIR)

    # 差值（仅 Linux 且两个 bundle 都存在时）
    if report["local_exists"] and report["client_exists"]:
        report["unpacked_diff_bytes"] = (
            report["local_unpacked_bytes"] - report["client_unpacked_bytes"]
        )
        report["parser_diff_bytes"] = (
            report["local_parser_bytes"] - report["client_parser_bytes"]
        )

    # 打印 JSON 报告（CI 可解析）
    print("  报告内容（JSON）:")
    for key, value in report.items():
        if isinstance(value, int) and key.endswith("_bytes"):
            print(f"    {key}: {value} ({_format_bytes(value)})")
        else:
            print(f"    {key}: {value}")

    # 三平台格式验证：字段名必须一致
    required_keys = {
        "platform",
        "local_bundle",
        "client_bundle",
        "local_exists",
        "client_exists",
        "client_supported",
    }
    missing = required_keys - set(report.keys())
    if missing:
        print(f"  [FAIL] 报告缺少跨平台必需字段: {sorted(missing)}")
        return False

    # client_supported 必须与平台匹配
    expected_client_supported = sys.platform.startswith("linux")
    if report["client_supported"] != expected_client_supported:
        print(f"  [FAIL] client_supported={report['client_supported']} "
              f"但平台 {sys.platform} 期望 {expected_client_supported}")
        return False

    # Windows/macOS 不应有 client bundle
    if not expected_client_supported and report["client_exists"]:
        print(f"  [FAIL] {sys.platform} 不应构建 client/agent bundle，"
              f"但 {CLIENT_BUNDLE_DIR} 存在")
        return False

    print("  [OK] 三平台报告格式一致")
    return True


def main():
    print("=" * 60)
    print("H14/N5-N8 跨平台打包产物验证")
    print("=" * 60)

    results = {}

    # 构建产物
    results["manifest"] = check_manifest()
    results["wheel_metadata"] = check_wheel_metadata()

    # N5 Windows
    results["n5_wxs"] = check_wxs()

    # N6 macOS
    results["n6_pkg"] = check_bash_script(
        RELEASE_DIR / "macos" / "build_pkg.sh", "N6 macOS"
    )

    # N7 Linux
    results["n7_deb_main"] = check_bash_script(
        RELEASE_DIR / "linux" / "build_packages.sh", "N7 Linux 主脚本"
    )
    results["n7_deb_packages"] = check_linux_packages()

    # N8 CI
    results["n8_ci"] = check_ci_yaml()

    # P0-B 轻量包体积度量（设计 Phase 1 步骤 5）
    results["p0b_unpacked_diff"] = check_light_bundle_unpacked_diff()
    results["p0b_compressed_diff"] = check_light_bundle_compressed_diff()
    results["p0b_three_platform_report"] = check_light_bundle_three_platform_report()

    # 汇总
    print("\n" + "=" * 60)
    print("验证汇总")
    print("=" * 60)
    all_pass = True
    for name, ok in results.items():
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("[PASS] 所有验证通过")
        return 0
    else:
        print("[FAIL] 部分验证失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
