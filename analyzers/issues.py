"""缺陷检测和 Semgrep 静态分析 Mixin 类。

提供函数级别的缺陷检测（基于正则规则）和 Semgrep 多语言静态分析功能。
"""

from typing import Any, Dict, List, Optional
import os
import json
import time

from ..config import detect_language_from_path as config_detect_language
from ..i18n import t


class IssueAnalyzerMixin:
    """缺陷检测和 Semgrep 静态分析 Mixin 类。
    
    包含以下功能：
    - 基于正则规则的函数缺陷检测（按语言分组）
    - 缺陷类型汇总统计
    - Semgrep CLI 多语言静态分析
    - Semgrep 结果入库与符号关联
    """

    # ===== 按语言分组的缺陷检测规则 =====
    # 每条规则: (issue_key, issue_label, severity, regex_pattern, description)
    # 通用规则（所有语言适用）
    COMMON_ISSUE_RULES = [
        ("todo_fixme",        "TODO/FIXME",     "warn",
         r'\b(TODO|FIXME|HACK|XXX|WORKAROUND)\b',
         "代码中包含 TODO/FIXME/HACK 等未完成标记"),
        ("hardcoded_path",    "硬编码路径",      "warn",
         r'"(?:src|docs|scripts|tests|target|\.cargo|\.git|/tmp/|/usr/|/etc/|C:\\|/home/)[/\w\-\.\\]*"',
         "硬编码了文件路径，建议使用配置文件或环境变量"),
        ("hardcoded_url",     "硬编码 URL",     "info",
         r'"https?://[^\s"]+"',
         "硬编码了 URL，建议使用配置文件"),
        ("magic_number",      "魔法数字",       "info",
         r'\b\d{4,}\b',
         "硬编码的大数字常量（4 位以上），建议定义为具名常量"),
    ]

    # Rust 专用规则
    RUST_ISSUE_RULES = [
        ("unwrap_call",       "unwrap 调用",    "danger",
         r'\.unwrap\(\)',
         "使用 unwrap()，可能导致 panic"),
        ("expect_call",       "expect 调用",    "warn",
         r'\.expect\(',
         "使用 expect()，可能导致 panic"),
        ("panic_macro",       "panic!/unimplemented!",    "danger",
         r'\bpanic!\(|\bunimplemented!\(|\btodo!\(\)',
         "包含 panic!/unimplemented!/todo!() 占位代码"),
        ("unsafe_block",      "unsafe 块",      "warn",
         r'\bunsafe\s*\{|\bunsafe\s+fn\b|\bunsafe\s+impl\b',
         "包含 unsafe 代码块"),
        ("clone_heavy",       "频繁 clone",    "info",
         r'\.clone\(\)',
         "频繁使用 clone()，可能影响性能"),
    ]

    # TypeScript/JavaScript 专用规则
    TYPESCRIPT_ISSUE_RULES = [
        ("any_type",          "any 类型",       "warn",
         r':\s*any\b|\bas\s+any\b',
         "使用 any 类型，失去类型安全"),
        ("console_log",       "console.log",    "info",
         r'console\.(log|error|warn|debug)\(',
         "包含 console 调试输出，建议移除"),
        ("ts_ignore",         "@ts-ignore",    "warn",
         r'@ts-ignore|@ts-nocheck',
         "使用 @ts-ignore 跳过类型检查"),
        ("non_null_assertion","非空断言",       "warn",
         r'\w!\s*[.\(\)]',
         "使用非空断言操作符 (!)，可能导致运行时错误"),
    ]

    # Python 专用规则
    PYTHON_ISSUE_RULES = [
        ("bare_except",       "裸 except",      "danger",
         r'except\s*:',
         "使用裸 except，会捕获所有异常包括 SystemExit"),
        ("print_debug",       "print 调试",     "info",
         r'\bprint\(',
         "包含 print 调试输出，建议使用 logging"),
        ("broad_except",      "宽泛异常",       "warn",
         r'except\s+Exception\s*:',
         "捕获 Exception 过于宽泛，建议精确捕获"),
        ("mutable_default",   "可变默认参数",   "warn",
         r'def\s+\w+\([^)]*=\s*(\[\]|\{\})',
         "使用可变对象作为默认参数，可能导致意外行为"),
    ]

    # Kotlin 专用规则
    KOTLIN_ISSUE_RULES = [
        ("!!_operator",       "!! 非空断言",    "warn",
         r'\w\s*!!',
         "使用 !! 非空断言，可能导致 NullPointerException"),
        ("println_debug",     "println 调试",   "info",
         r'\bprintln\(',
         "包含 println 调试输出"),
        ("unsafe_cast",       "不安全类型转换",  "warn",
         r'\bas\s+\w',
         "使用不安全的类型转换，建议用安全转换 as?"),
    ]

    # Go 专用规则
    GO_ISSUE_RULES = [
        ("nil_check_missing", "缺少 nil 检查",  "warn",
         r'\.([A-Z]\w*)\(',
         "可能缺少 nil 检查（调用方法前未判断 nil）"),
        ("err_ignored",       "忽略错误返回",   "danger",
         r'_\s*=\s*\w+\.\w+\(',
         "使用 _ 忽略了错误返回值，建议处理错误"),
        ("fmt_print_debug",   "fmt.Print 调试", "info",
         r'fmt\.Print(ln|f)?\(',
         "包含 fmt.Print 调试输出，建议使用日志库"),
        ("panic_call",        "panic 调用",    "danger",
         r'\bpanic\(',
         "使用 panic()，可能导致程序崩溃"),
        ("unsafe_import",     "unsafe 包",     "warn",
         r'"unsafe"',
         "导入了 unsafe 包，可能存在内存安全风险"),
    ]

    # Java 专用规则
    JAVA_ISSUE_RULES = [
        ("print_stack_trace", "printStackTrace", "warn",
         r'\.printStackTrace\(',
         "使用 printStackTrace()，建议使用日志框架"),
        ("system_out_print",  "System.out 调试", "info",
         r'System\.out\.print(ln)?\(',
         "包含 System.out 调试输出，建议使用日志框架"),
        ("empty_catch",       "空 catch 块",    "warn",
         r'catch\s*\([^)]+\)\s*\{[\s;]*\}',
         "空的 catch 块，异常被静默吞掉"),
        ("raw_type",          "原始类型",       "warn",
         r'\b(List|Map|Set|Collection|Iterator|Comparable)\s*[^<\w]',
         "使用原始类型（未指定泛型参数），失去类型安全"),
        ("magic_suppress",    "@SuppressWarnings", "info",
         r'@SuppressWarnings',
         "使用 @SuppressWarnings 抑制警告"),
    ]

    # C/C++ 专用规则（两种语言共用）
    C_CPP_ISSUE_RULES = [
        ("printf_debug",      "printf 调试",    "info",
         r'\bprintf\(|\bfprintf\(',
         "包含 printf/fprintf 调试输出"),
        ("unsafe_malloc",     "malloc 不检查",  "danger",
         r'=\s*malloc\s*\(',
         "调用 malloc 后未检查返回值是否为 NULL"),
        ("unsafe_free",       "野指针风险",     "warn",
         r'free\s*\([^)]+\);',
         "free 后未置空指针，可能导致野指针"),
        ("goto_statement",    "goto 语句",      "warn",
         r'\bgoto\s+\w+',
         "使用 goto 语句，影响代码可读性"),
        ("char_buffer",       "char 数组溢出",  "warn",
         r'char\s+\w+\s*\[\d+\]',
         "固定大小 char 数组，可能存在缓冲区溢出风险"),
        ("void_star",         "void* 指针",     "warn",
         r'void\s*\*',
         "使用 void* 指针，失去类型安全"),
        ("define_macro",      "宏定义",         "info",
         r'#define\s+\w+',
         "使用宏定义，建议用 const/constexpr 替代"),
    ]

    LANGUAGE_RULES_MAP = {
        "rust": RUST_ISSUE_RULES,
        "typescript": TYPESCRIPT_ISSUE_RULES,
        "javascript": TYPESCRIPT_ISSUE_RULES,
        "python": PYTHON_ISSUE_RULES,
        "kotlin": KOTLIN_ISSUE_RULES,
        "go": GO_ISSUE_RULES,
        "java": JAVA_ISSUE_RULES,
        "c": C_CPP_ISSUE_RULES,
        "cpp": C_CPP_ISSUE_RULES,
    }

    def _get_issue_rules_for_language(self, language: str) -> List:
        """获取指定语言的所有规则（通用 + 语言专用）"""
        rules = list(self.COMMON_ISSUE_RULES)
        lang_rules = self.LANGUAGE_RULES_MAP.get(language, [])
        rules.extend(lang_rules)
        return rules

    def _get_all_issue_rules(self) -> List:
        """获取所有语言的规则全集（通用 + 所有语言专用，按 issue_key 去重）

        用于统计场景：需要遍历所有可能的缺陷类型，但不知道具体语言时使用。
        去重规则：相同 issue_key 只保留首次出现（通用规则优先于语言专用规则）。
        """
        seen_keys = set()
        all_rules: List = []
        # 先加通用规则
        for rule in self.COMMON_ISSUE_RULES:
            if rule[0] not in seen_keys:
                seen_keys.add(rule[0])
                all_rules.append(rule)
        # 再加各语言专用规则（去重）
        for lang_rules in self.LANGUAGE_RULES_MAP.values():
            for rule in lang_rules:
                if rule[0] not in seen_keys:
                    seen_keys.add(rule[0])
                    all_rules.append(rule)
        return all_rules

    def _detect_language_from_module_path(self, module_path: str) -> str:
        """从 module_path 推断语言（用于按语言选择缺陷检测规则）

        启发式规则（按优先级）：
        1. 先匹配文件扩展名（最准确）：.py/.go/.java/.ts/.js/.c/.cpp/.kt/.rs
        2. 再匹配路径分隔符：含 "::" 且无文件扩展名 → rust
        3. 默认 → "rust"（本项目主语言，作为兜底）
        """
        if not module_path:
            return "rust"
        mp_lower = module_path.lower()
        # 1. 优先按文件扩展名判断（最准确）
        if ".py" in mp_lower:
            return "python"
        if ".go" in mp_lower:
            return "go"
        if ".java" in mp_lower:
            return "java"
        if ".tsx" in mp_lower:
            return "typescript"
        if ".ts" in mp_lower:
            return "typescript"
        if ".jsx" in mp_lower:
            return "javascript"
        if ".js" in mp_lower:
            return "javascript"
        if ".cpp" in mp_lower or ".cc" in mp_lower or ".cxx" in mp_lower:
            return "cpp"
        if ".kt" in mp_lower:
            return "kotlin"
        if ".rs" in mp_lower:
            return "rust"
        if ".c" in mp_lower:
            return "c"
        # 2. 无文件扩展名时，用 "::" 判断 rust（Rust 模块路径特征）
        if "::" in mp_lower:
            return "rust"
        return "rust"

    def get_function_issues(self, qualified_name: str = "", module_filter: str = "",
                            issue_filter: str = "", limit: int = 50) -> List[Dict]:
        """检测函数缺陷
        
        Args:
            qualified_name: 指定函数（为空则扫描全部）
            module_filter: 模块过滤（前缀匹配）
            issue_filter: 只显示指定缺陷类型
            limit: 返回数量限制
            
        Returns:
            有缺陷的函数列表，包含缺陷详情
        """
        import re
        
        # 构建查询
        sql = """
            SELECT DISTINCT fsv.qualified_name, fsv.module_path, sc.content, sc.has_comment, sc.name
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fv.is_current = 1 AND sc.kind = 'fn'
        """
        params = []
        
        if qualified_name:
            sql += " AND fsv.qualified_name = ?"
            params.append(qualified_name)
        
        if module_filter:
            sql += " AND fsv.module_path LIKE ?"
            params.append(module_filter + "%")
        
        # 排除 test 函数（默认不分析测试函数的缺陷）
        if not qualified_name:
            sql += " AND fsv.module_path NOT LIKE '%::tests' AND sc.name NOT LIKE 'test_%'"
        
        sql += " LIMIT ?"
        params.append(limit * 5)  # 多取一些，后面在 Python 中过滤
        
        cur = self.conn.execute(sql, params)
        
        results = []
        for row in cur:
            content = row["content"] or ""
            issues = []

            # 按符号的 module_path 推断语言，选择对应的缺陷检测规则
            language = self._detect_language_from_module_path(row["module_path"] or "")
            issue_rules = self._get_issue_rules_for_language(language)

            for issue_key, label, severity, pattern, desc in issue_rules:
                if issue_filter and issue_key != issue_filter:
                    continue
                
                if issue_key == "missing_comment":
                    # 特殊处理：用 has_comment 字段
                    if row["has_comment"] == 0:
                        # 排除 main 函数和极短函数（< 3 行）
                        line_count = content.count("\n")
                        if line_count >= 3:
                            issues.append({
                                "type": issue_key,
                                "label": label,
                                "severity": severity,
                                "count": 1,
                                "description": desc,
                            })
                else:
                    # 正则匹配
                    matches = re.findall(pattern, content, re.MULTILINE)
                    if matches:
                        issues.append({
                            "type": issue_key,
                            "label": label,
                            "severity": severity,
                            "count": len(matches),
                            "description": desc,
                        })
            
            if issues:
                results.append({
                    "qualified_name": row["qualified_name"],
                    "module_path": row["module_path"] or "",
                    "name": row["name"],
                    "issue_count": len(issues),
                    "issues": issues,
                })
        
        # 按缺陷数量排序
        results.sort(key=lambda x: x["issue_count"], reverse=True)
        return results[:limit]

    def get_issue_summary(self, module_filter: str = "") -> Dict:
        """获取缺陷类型汇总统计
        
        Args:
            module_filter: 模块过滤（前缀匹配）
            
        Returns:
            按缺陷类型汇总的统计数据
        """
        import re
        
        # 获取所有非测试函数
        sql = """
            SELECT DISTINCT fsv.qualified_name, fsv.module_path, sc.content, sc.has_comment, sc.name
            FROM file_symbol_versions fsv
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fv.is_current = 1 AND sc.kind = 'fn'
              AND fsv.module_path NOT LIKE '%::tests' AND sc.name NOT LIKE 'test_%'
        """
        params = []
        if module_filter:
            sql += " AND fsv.module_path LIKE ?"
            params.append(module_filter + "%")
        
        cur = self.conn.execute(sql, params)
        
        # 初始化统计（用所有语言规则全集，确保任何语言的缺陷都能被统计）
        issue_stats = {}
        all_rules = self._get_all_issue_rules()
        for issue_key, label, severity, _, desc in all_rules:
            issue_stats[issue_key] = {
                "label": label,
                "severity": severity,
                "description": desc,
                "function_count": 0,
                "total_occurrences": 0,
            }
        
        total_functions = 0
        functions_with_issues = 0
        
        for row in cur:
            total_functions += 1
            content = row["content"] or ""
            has_any_issue = False

            # 按符号语言选择规则（避免误匹配其他语言的规则）
            language = self._detect_language_from_module_path(row["module_path"] or "")
            row_rules = self._get_issue_rules_for_language(language)

            for issue_key, label, severity, pattern, desc in row_rules:
                if issue_key == "missing_comment":
                    if row["has_comment"] == 0:
                        line_count = content.count("\n")
                        if line_count >= 3:
                            issue_stats[issue_key]["function_count"] += 1
                            issue_stats[issue_key]["total_occurrences"] += 1
                            has_any_issue = True
                else:
                    matches = re.findall(pattern, content, re.MULTILINE)
                    if matches:
                        issue_stats[issue_key]["function_count"] += 1
                        issue_stats[issue_key]["total_occurrences"] += len(matches)
                        has_any_issue = True
            
            if has_any_issue:
                functions_with_issues += 1
        
        # 转换为列表并按函数数排序
        issue_list = []
        for key, stats in issue_stats.items():
            issue_list.append({
                "type": key,
                **stats,
                "ratio": round(stats["function_count"] / total_functions * 100, 1) if total_functions > 0 else 0,
            })
        issue_list.sort(key=lambda x: x["function_count"], reverse=True)
        
        return {
            "total_functions": total_functions,
            "functions_with_issues": functions_with_issues,
            "issue_free_functions": total_functions - functions_with_issues,
            "issue_free_ratio": round((total_functions - functions_with_issues) / total_functions * 100, 1) if total_functions > 0 else 0,
            "issues": issue_list,
        }

    def run_semgrep(self, target_paths: List[str], config: str = "p/default",
                    languages: List[str] = None, timeout: int = 300) -> Dict:
        """使用 Semgrep CLI 进行多语言静态分析
        
        Args:
            target_paths: 要扫描的路径列表（文件或目录）
            config: Semgrep 规则配置（p/default, p/security, p/best-practices 等）
            languages: 限制扫描的语言列表（None 表示自动检测）
            timeout: 超时时间（秒）
            
        Returns:
            Semgrep 扫描结果（结构化 JSON）
        """
        import subprocess
        import shutil
        
        # 找到 Semgrep CLI 路径
        semgrep_path = self._find_semgrep_cli()
        if not semgrep_path:
            return {
                "success": False,
                "error": t("cli.messages.semgrep_cli_not_found_install", default="Semgrep CLI not found. Please run: pip install semgrep"),
                "results": [],
            }
        
        # 构建命令
        cmd = [semgrep_path, "--config", config, "--json", "--quiet"]
        
        if languages:
            # config 模式下用 --include 限制文件扩展名（等价于限制语言）
            ext_map = {
                "rust": ["*.rs"],
                "typescript": ["*.ts", "*.tsx"],
                "javascript": ["*.js", "*.jsx"],
                "python": ["*.py"],
                "kotlin": ["*.kt", "*.kts"],
                "go": ["*.go"],
                "java": ["*.java"],
                "c": ["*.c", "*.h"],
                "cpp": ["*.cpp", "*.hpp", "*.cc", "*.hh"],
            }
            for lang in languages:
                exts = ext_map.get(lang.lower(), [])
                for ext in exts:
                    cmd.extend(["--include", ext])
        
        cmd.extend(target_paths)
        
        try:
            # 设置 PATH 环境变量（确保 pysemgrep 在 PATH 中）
            env = os.environ.copy()
            scripts_dir = os.path.dirname(semgrep_path)
            if scripts_dir not in env.get("PATH", ""):
                env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                env=env,
                cwd=self.workspace_root,
            )
            
            if result.returncode not in (0, 1):  # 0=无发现, 1=有发现, 其他=错误
                return {
                    "success": False,
                    "error": f"Semgrep 返回码 {result.returncode}: {result.stderr[:500]}",
                    "results": [],
                }
            
            # 解析 JSON 输出
            data = json.loads(result.stdout) if result.stdout else {}
            
            # 整理结果
            findings = []
            for item in data.get("results", []):
                finding = {
                    "rule_id": item.get("check_id", ""),
                    "rule_name": item.get("check_id", "").split(".")[-1] if item.get("check_id") else "",
                    "message": item.get("extra", {}).get("message", ""),
                    "severity": item.get("extra", {}).get("severity", "INFO"),
                    "confidence": item.get("extra", {}).get("confidence", "UNKNOWN"),
                    "path": item.get("path", ""),
                    "start_line": item.get("start", {}).get("line", 0),
                    "end_line": item.get("end", {}).get("line", 0),
                    "snippet": item.get("extra", {}).get("lines", ""),
                    "language": self._detect_language_from_path(item.get("path", "")),
                    "fix": item.get("extra", {}).get("fix", ""),
                    "references": item.get("extra", {}).get("references", []),
                }
                findings.append(finding)
            
            # 按严重程度分类统计
            severity_counts = {}
            for f in findings:
                sev = f["severity"]
                severity_counts[sev] = severity_counts.get(sev, 0) + 1
            
            return {
                "success": True,
                "total_findings": len(findings),
                "severity_counts": severity_counts,
                "results": findings,
                "paths_scanned": data.get("paths", {}).get("scanned", []),
                "errors": data.get("errors", []),
            }
            
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Semgrep 扫描超时（{timeout}秒）",
                "results": [],
            }
        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error": f"Semgrep JSON 解析失败: {e}",
                "raw_output": result.stdout[:500] if result.stdout else "",
                "results": [],
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Semgrep 执行异常: {e}",
                "results": [],
            }

    def _find_semgrep_cli(self) -> str:
        """查找 Semgrep CLI 路径"""
        import shutil
        
        # 优先使用 PATH 中的 semgrep
        semgrep_in_path = shutil.which("semgrep")
        if semgrep_in_path:
            return semgrep_in_path
        
        # Windows: 检查 Python Scripts 目录
        import site
        for site_path in site.getsitepackages():
            scripts_dir = os.path.join(os.path.dirname(site_path), "Scripts")
            semgrep_exe = os.path.join(scripts_dir, "semgrep.exe")
            if os.path.exists(semgrep_exe):
                return semgrep_exe
            
        # 用户 site-packages
        user_site = site.getusersitepackages()
        if user_site:
            scripts_dir = os.path.join(os.path.dirname(user_site), "Scripts")
            semgrep_exe = os.path.join(scripts_dir, "semgrep.exe")
            if os.path.exists(semgrep_exe):
                return semgrep_exe
        
        return ""

    def _detect_language_from_path(self, path: str) -> str:
        """从文件路径推断语言（委托给 config 模块的统一检测逻辑）"""
        return config_detect_language(path)

    def get_semgrep_summary(self, target_paths: List[str] = None) -> Dict:
        """使用 Semgrep 快速扫描获取缺陷概览
        
        Args:
            target_paths: 扫描路径（默认扫描整个 workspace）
            
        Returns:
            按 severity/language 分类的问题统计
        """
        if not target_paths:
            target_paths = [self.workspace_root]
        
        result = self.run_semgrep(target_paths, config="p/default", timeout=180)
        
        if not result.get("success"):
            return result
        
        # 按 severity/language 分组统计
        by_severity = {}
        by_language = {}
        by_rule = {}
        
        for f in result["results"]:
            sev = f["severity"]
            lang = f["language"]
            rule = f["rule_id"]
            
            by_severity[sev] = by_severity.get(sev, 0) + 1
            by_language[lang] = by_language.get(lang, 0) + 1
            
            if rule not in by_rule:
                by_rule[rule] = {"count": 0, "message": f["message"], "severity": sev}
            by_rule[rule]["count"] += 1
        
        # 按数量排序
        by_rule_list = sorted(by_rule.items(), key=lambda x: x[1]["count"], reverse=True)
        
        return {
            "success": True,
            "total_findings": result["total_findings"],
            "by_severity": by_severity,
            "by_language": by_language,
            "top_rules": by_rule_list[:20],
            "errors": result.get("errors", []),
        }

    # --------------------------------------------------------------------
    # Semgrep 结果入库与查询
    # --------------------------------------------------------------------

    def save_semgrep_findings(self, findings: List[Dict], scan_config: str = "p/default",
                              scan_type: str = "full", files_scanned: int = 0,
                              stale_file_ids: Optional[List[int]] = None) -> int:
        """将 Semgrep 扫描结果存入数据库，并关联到符号

        A14 修复（2026-07-20）：新增 scan_type / files_scanned / stale_file_ids 参数，
        支持 'incremental' 增量扫描语义：
        - scan_type='incremental' 时，stale_file_ids 中的 file_instance_id 的旧 findings
          会被清除（避免变更文件出现新旧两份 findings 重复计数）
        - scan_id 写入每条 finding，让后续审计能追溯到具体某次扫描

        Args:
            findings: Semgrep 发现的问题列表
            scan_config: 使用的规则配置
            scan_type: 扫描类型 'full'（全量）/ 'incremental'（增量）
            files_scanned: 本次扫描的文件数（写入 semgrep_scans.files_scanned）
            stale_file_ids: 增量扫描时需清理旧 findings 的 file_instance_id 列表；
                仅 scan_type='incremental' 时生效

        Returns:
            存入的问题数量
        """
        from ..config import norm_path

        ws_id = self._get_active_workspace_id()
        scanned_at = time.time()
        count = 0

        # 记录扫描（A14：scan_type 参数化）
        cur = self.conn.execute(
            """INSERT INTO semgrep_scans
               (scan_type, config, workspace_id, started_at, status, files_scanned)
               VALUES (?, ?, ?, ?, 'completed', ?)""",
            (scan_type, scan_config, ws_id, scanned_at, files_scanned),
        )
        scan_id = cur.lastrowid

        # A14：增量扫描清理旧 findings
        # 变更文件可能有旧 findings（content_hash 不同，UNIQUE 约束不会去重），
        # 直接清掉这批 file_instance_id 的所有 findings（保留本次即将插入的）
        if scan_type == "incremental" and stale_file_ids:
            # 防御式：先批量删除变更文件的旧 findings（含本次 scan 即将插入的也会被删，
            # 但下面 INSERT OR IGNORE 会重新插入，最终态正确）
            batch_size = 500
            for i in range(0, len(stale_file_ids), batch_size):
                chunk = stale_file_ids[i:i + batch_size]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"DELETE FROM semgrep_findings WHERE file_instance_id IN ({placeholders})",
                    chunk,
                )

        # 获取文件实例 id 映射（当前工作区）
        file_map = {}
        cur = self.conn.execute(
            "SELECT id, rel_path, current_content_hash FROM file_instances WHERE workspace_id = ?",
            (ws_id,),
        )
        for row in cur:
            file_map[row["rel_path"]] = {
                "id": row["id"],
                "content_hash": row["current_content_hash"],
            }

        # 获取当前版本的符号位置（用于关联）
        symbol_positions = self._get_current_symbol_positions()

        for f in findings:
            fpath = norm_path(f.get("path", ""))
            file_info = file_map.get(fpath)
            if not file_info:
                continue  # 跳过未注册的文件

            file_instance_id = file_info["id"]
            content_hash = file_info.get("content_hash", "")

            # 尝试关联到符号
            symbol_id = 0
            symbol_qualified = ""
            start_line = f.get("start_line", 0)
            if fpath in symbol_positions:
                for sym in symbol_positions[fpath]:
                    if sym["start_line"] <= start_line <= sym["end_line"]:
                        symbol_id = sym["id"]
                        symbol_qualified = sym["qualified_name"]
                        break

            self.conn.execute(
                """INSERT OR IGNORE INTO semgrep_findings
                   (file_instance_id, content_hash, rule_id, rule_name, message, severity, confidence,
                    language, start_line, end_line, snippet, fix,
                    symbol_id, symbol_qualified, scanned_at, scan_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_instance_id, content_hash, f.get("rule_id", ""), f.get("rule_name", ""),
                 f.get("message", ""), f.get("severity", "INFO"),
                 f.get("confidence", "UNKNOWN"), f.get("language", ""),
                 start_line, f.get("end_line", 0), f.get("snippet", ""),
                 f.get("fix", ""), symbol_id, symbol_qualified, scanned_at, scan_id),
            )
            count += 1

        # 更新扫描记录
        self.conn.execute(
            "UPDATE semgrep_scans SET completed_at = ?, total_findings = ?, status = 'completed' WHERE id = ?",
            (time.time(), count, scan_id),
        )
        self.conn.commit()
        return count

    def _get_current_symbol_positions(self) -> Dict[str, List[Dict]]:
        """获取当前所有文件的符号位置（用于关联 Semgrep 结果到符号）"""
        ws_id = self._get_active_workspace_id()
        positions: Dict[str, List[Dict]] = {}

        cur = self.conn.execute(
            """SELECT s.id, s.qualified_name, s.start_line, s.end_line, fi.rel_path
               FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ?
                 AND s.kind IN ('fn', 'method', 'class', 'struct', 'enum', 'trait', 'interface')""",
            (ws_id,),
        )
        for row in cur:
            fpath = row["rel_path"]
            if fpath not in positions:
                positions[fpath] = []
            positions[fpath].append({
                "id": row["id"],
                "qualified_name": row["qualified_name"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
            })

        # 按 start_line 排序便于查找
        for fpath in positions:
            positions[fpath].sort(key=lambda x: x["start_line"])

        return positions

    def get_semgrep_findings(self, severity: str = "", language: str = "",
                             rule_id: str = "", symbol_qualified: str = "",
                             limit: int = 100) -> List[Dict]:
        """查询 Semgrep 发现的问题

        Args:
            severity: 按严重程度过滤（ERROR/WARNING/INFO）
            language: 按语言过滤
            rule_id: 按规则 ID 过滤
            symbol_qualified: 按符号限定名过滤
            limit: 返回数量限制

        Returns:
            发现的问题列表
        """
        ws_id = self._get_active_workspace_id()
        sql = """
            SELECT sf.*, fi.rel_path as file_path
            FROM semgrep_findings sf JOIN file_instances fi ON sf.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """
        params = [ws_id]

        if severity:
            sql += " AND sf.severity = ?"
            params.append(severity.upper())

        if language:
            sql += " AND sf.language = ?"
            params.append(language)

        if rule_id:
            sql += " AND sf.rule_id LIKE ?"
            params.append("%" + rule_id + "%")

        if symbol_qualified:
            sql += " AND sf.symbol_qualified = ?"
            params.append(symbol_qualified)

        sql += " ORDER BY sf.severity = 'ERROR' DESC, sf.severity = 'WARNING' DESC, sf.id DESC LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur]

    def get_semgrep_stats(self) -> Dict:
        """获取 Semgrep 缺陷统计"""
        ws_id = self._get_active_workspace_id()
        stats = {"by_severity": {}, "by_language": {}, "by_rule": [],
                 "by_symbol": [], "total_findings": 0}

        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM semgrep_findings sf
            JOIN file_instances fi ON sf.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        stats["total_findings"] = cur.fetchone()["cnt"]

        cur = self.conn.execute("""
            SELECT sf.severity, COUNT(*) as cnt
            FROM semgrep_findings sf
            JOIN file_instances fi ON sf.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            GROUP BY sf.severity ORDER BY cnt DESC
        """, (ws_id,))
        stats["by_severity"] = {row["severity"]: row["cnt"] for row in cur}

        cur = self.conn.execute("""
            SELECT sf.language, COUNT(*) as cnt
            FROM semgrep_findings sf
            JOIN file_instances fi ON sf.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            GROUP BY sf.language ORDER BY cnt DESC
        """, (ws_id,))
        stats["by_language"] = {row["language"]: row["cnt"] for row in cur}

        cur = self.conn.execute("""
            SELECT sf.rule_id, sf.rule_name, sf.severity, COUNT(*) as cnt
            FROM semgrep_findings sf
            JOIN file_instances fi ON sf.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            GROUP BY sf.rule_id ORDER BY cnt DESC LIMIT 20
        """, (ws_id,))
        stats["by_rule"] = [dict(row) for row in cur]

        cur = self.conn.execute("""
            SELECT sf.symbol_qualified, COUNT(*) as cnt
            FROM semgrep_findings sf
            JOIN file_instances fi ON sf.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND sf.symbol_qualified != ''
            GROUP BY sf.symbol_qualified ORDER BY cnt DESC LIMIT 20
        """, (ws_id,))
        stats["by_symbol"] = [dict(row) for row in cur]

        return stats

    def run_semgrep_and_save(self, target_paths: List[str] = None,
                             config: str = "p/default",
                             languages: List[str] = None,
                             timeout: int = 300) -> Dict:
        """运行 Semgrep 并将结果存入数据库

        Args:
            target_paths: 扫描路径
            config: 规则配置
            languages: 语言过滤
            timeout: 超时时间

        Returns:
            扫描结果摘要
        """
        if not target_paths:
            target_paths = [self.workspace_root]

        result = self.run_semgrep(target_paths, config=config,
                                  languages=languages, timeout=timeout)

        if not result.get("success"):
            return result

        count = self.save_semgrep_findings(result.get("results", []), config)

        return {
            "success": True,
            "saved_findings": count,
            "total_findings": result["total_findings"],
        }

    def scan_semgrep_incremental(self, base_branch: str = "main",
                                 head: str = "HEAD",
                                 config: str = "p/default",
                                 languages: List[str] = None,
                                 timeout: int = 300) -> Dict:
        """增量 Semgrep 扫描：只扫描 git diff 变更文件并清理旧 findings

        A14 修复（2026-07-20 二轮评审）：
        - 旧实现 scan_type 硬编码 'full'，无增量扫描语义
        - 旧 schema semgrep_findings 无 scan_id 字段，无法关联 finding 到 scan
        - 旧 cicd/pr_check.py 虽传 target_paths=changed_files，但不清理旧 findings，
          导致变更文件出现新旧两份 findings 重复计数

        修复：
        1. 通过 IncrementalAnalyzer.get_changed_files() 取 base_branch...head 的变更文件
        2. 调用 run_semgrep() 扫描变更文件
        3. save_semgrep_findings(scan_type='incremental', stale_file_ids=...)
           - 写 semgrep_scans.scan_type='incremental'
           - 写 semgrep_findings.scan_id 关联到本次扫描
           - 删除变更文件的旧 findings（stale_file_ids）

        Args:
            base_branch: 基准分支（默认 main）
            head: 目标提交（默认 HEAD）
            config: 规则配置（默认 p/default）
            languages: 语言过滤
            timeout: 超时时间（秒）

        Returns:
            {
                "success": bool,
                "scan_type": "incremental",
                "base_branch": str,
                "head": str,
                "changed_files": int,        # git diff 变更文件数
                "scanned_files": int,        # 实际扫描的文件数
                "saved_findings": int,       # 本次入库 findings 数
                "total_findings": int,      # Semgrep 报告的 findings 总数
                "stale_file_ids": int,      # 清理旧 findings 涉及的文件数
            }
        """
        from ..cicd.incremental import IncrementalAnalyzer

        # 1. 取 git diff 变更文件
        incremental = IncrementalAnalyzer(self)
        changed_files = incremental.get_changed_files(
            base_branch=base_branch, head=head
        )

        if not changed_files:
            return {
                "success": True,
                "scan_type": "incremental",
                "base_branch": base_branch,
                "head": head,
                "changed_files": 0,
                "scanned_files": 0,
                "saved_findings": 0,
                "total_findings": 0,
                "stale_file_ids": 0,
            }

        # 2. 把变更文件路径转为绝对路径（run_semgrep 接受绝对路径）
        from ..config import norm_path
        abs_paths = []
        for rel_path in changed_files:
            abs_path = norm_path(os.path.join(self.workspace_root, rel_path))
            if os.path.exists(abs_path):
                abs_paths.append(abs_path)

        # 3. 找出已注册的 file_instance_id（用于清理旧 findings）
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            "SELECT id, rel_path FROM file_instances WHERE workspace_id = ? AND status != 'deleted'",
            (ws_id,),
        )
        registered_paths = {row["rel_path"]: row["id"] for row in cur.fetchall()}
        stale_file_ids = [
            registered_paths[rel_path]
            for rel_path in changed_files
            if rel_path in registered_paths
        ]

        # 4. 运行 Semgrep 扫描
        result = self.run_semgrep(abs_paths, config=config,
                                  languages=languages, timeout=timeout)

        if not result.get("success"):
            return {
                "success": False,
                "scan_type": "incremental",
                "base_branch": base_branch,
                "head": head,
                "changed_files": len(changed_files),
                "scanned_files": len(abs_paths),
                "saved_findings": 0,
                "total_findings": result.get("total_findings", 0),
                "stale_file_ids": len(stale_file_ids),
                "error": result.get("error", "Semgrep run failed"),
            }

        # 5. 入库 + 清理旧 findings
        count = self.save_semgrep_findings(
            result.get("results", []),
            config,
            scan_type="incremental",
            files_scanned=len(abs_paths),
            stale_file_ids=stale_file_ids,
        )

        return {
            "success": True,
            "scan_type": "incremental",
            "base_branch": base_branch,
            "head": head,
            "changed_files": len(changed_files),
            "scanned_files": len(abs_paths),
            "saved_findings": count,
            "total_findings": result["total_findings"],
            "stale_file_ids": len(stale_file_ids),
        }
