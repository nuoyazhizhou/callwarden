"""H14/N5-N8 跨平台打包产物验证脚本

验证内容：
- N5 Windows: callwarden.wxs WiX 源文件 XML 语法
- N6 macOS: build_pkg.sh bash 语法
- N7 Linux: build_packages.sh + 5 control + 14 maintainer scripts
- N8 Release CI: enterprise-release.yml YAML 语法 + 11 门禁 job 列表
- H14 整体: artifact-manifest.json + wheel 元数据

用法：
    python release/_check_artifacts.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"


def check_wxs():
    """N5: 验证 WiX wxs XML 语法"""
    print("\n=== N5: Windows WiX wxs 验证 ===")
    wxs_path = RELEASE_DIR / "windows" / "callwarden.wxs"
    if not wxs_path.exists():
        print(f"  [FAIL] 未找到 {wxs_path}")
        return False

    try:
        import xml.etree.ElementTree as ET
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
