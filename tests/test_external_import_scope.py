"""外部符号导入范围测试。"""

import os
import tempfile
import types
import zipfile

from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def test_python_external_import_uses_project_deps_only(monkeypatch):
    db, root = _db_with_workspace()
    try:
        with open(os.path.join(root, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("requests==2.0.0\n")

        imported = []
        monkeypatch.setattr(
            db,
            "_get_installed_packages",
            lambda: {"requests": "2.0.0", "networkx": "3.0.0", "torch": "2.0.0"},
        )
        monkeypatch.setattr(
            db,
            "_import_python_package",
            lambda name, version: imported.append((name, version)) or 1,
        )

        created, skipped = db._import_external_packages_for_lang("python")
        assert created == 1
        assert skipped == 0
        assert imported == [("requests", "2.0.0")]
    finally:
        db.close()


def test_python_external_import_explicit_package_names(monkeypatch):
    db, _root = _db_with_workspace()
    try:
        imported = []
        monkeypatch.setattr(
            db,
            "_get_installed_packages",
            lambda: {"requests": "2.0.0", "networkx": "3.0.0"},
        )
        monkeypatch.setattr(
            db,
            "_import_python_package",
            lambda name, version: imported.append((name, version)) or 1,
        )

        created, skipped = db._import_external_packages_for_lang("python", ["networkx"])
        assert created == 1
        assert skipped == 0
        assert imported == [("networkx", "3.0.0")]
    finally:
        db.close()


def test_extract_python_package_symbols_does_not_recurse_modules():
    db, _root = _db_with_workspace()
    try:
        module = types.ModuleType("demo")
        module.fn = lambda: None
        module.Child = type("Child", (), {})
        submodule = types.ModuleType("demo.submodule")
        submodule.inner = lambda: None
        module.submodule = submodule

        created = db._extract_package_symbols("demo", "1.0.0", module, "")
        rows = [
            dict(r)
            for r in db.conn.execute(
                "SELECT qualified_name, symbol_kind FROM external_symbols ORDER BY qualified_name"
            )
        ]
        assert created == 2
        assert rows == [
            {"qualified_name": "demo.Child", "symbol_kind": "class"},
            {"qualified_name": "demo.fn", "symbol_kind": "fn"},
        ]
    finally:
        db.close()


def test_prune_external_symbols_keeps_project_deps_and_stdlib():
    db, root = _db_with_workspace()
    try:
        with open(os.path.join(root, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write("requests==2.0.0\n")
        rows = [
            ("stdlib", "3", "os.path.join"),
            ("requests", "2.0.0", "requests.get"),
            ("networkx", "3.0.0", "networkx.Graph"),
            ("ext-python-torch", "2.0.0", "torch.Tensor"),
        ]
        for pkg, version, qn in rows:
            db.conn.execute(
                """
                INSERT INTO external_symbols
                    (package_name, package_version, module_path, qualified_name,
                     symbol_name, symbol_kind, signature, docstring, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pkg, version, pkg, qn, qn.rsplit(".", 1)[-1], "fn", "", "", ""),
            )
            db.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg, version),
            )
        db.conn.commit()

        result = db.prune_external_symbols(keep_project_deps=True)
        remaining = [
            r["package_name"]
            for r in db.conn.execute("SELECT package_name FROM external_symbols ORDER BY package_name")
        ]
        assert result["deleted"] == 2
        assert remaining == ["requests", "stdlib"]
    finally:
        db.close()


def test_runtime_manifests_ignore_transitive_or_non_compile_deps():
    db, root = _db_with_workspace()
    try:
        cargo_path = os.path.join(root, "Cargo.toml")
        with open(cargo_path, "w", encoding="utf-8") as f:
            f.write(
                """
[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1.0"
tokio = { version = "1.40", features = ["rt"] }

[dev-dependencies]
pretty_assertions = "1.4"

[build-dependencies]
cc = "1.0"
"""
            )
        assert db._parse_rust_manifest(cargo_path) == {
            "serde": "1.0",
            "tokio": "1.40",
        }

        go_path = os.path.join(root, "go.mod")
        with open(go_path, "w", encoding="utf-8") as f:
            f.write(
                """
module example.com/demo

require (
    github.com/spf13/cobra v1.6.1
    golang.org/x/text v0.3.7 // indirect
)

require github.com/pkg/errors v0.9.1
require golang.org/x/sys v0.1.0 // indirect
"""
            )
        assert db._parse_go_manifest(go_path) == {
            "github.com/spf13/cobra": "v1.6.1",
            "github.com/pkg/errors": "v0.9.1",
        }

        package_path = os.path.join(root, "package.json")
        with open(package_path, "w", encoding="utf-8") as f:
            f.write(
                """
{
  "dependencies": {"react": "18.2.0"},
  "devDependencies": {"vite": "5.0.0"},
  "peerDependencies": {"typescript": "5.0.0"},
  "optionalDependencies": {"fsevents": "2.3.3"}
}
"""
            )
        assert db._parse_package_json(package_path) == {"react": "18.2.0"}

        composer_path = os.path.join(root, "composer.json")
        with open(composer_path, "w", encoding="utf-8") as f:
            f.write(
                """
{
  "require": {"monolog/monolog": "^3.0"},
  "require-dev": {"phpunit/phpunit": "^10.0"}
}
"""
            )
        assert db._parse_php_manifest(composer_path) == {
            "monolog/monolog": "^3.0"
        }

        gradle_path = os.path.join(root, "build.gradle")
        with open(gradle_path, "w", encoding="utf-8") as f:
            f.write(
                """
dependencies {
    implementation 'org.slf4j:slf4j-api:2.0.0'
    api("com.google.guava:guava:33.0.0")
    compileOnly 'jakarta.servlet:jakarta.servlet-api:6.0.0'
    runtimeOnly 'org.postgresql:postgresql:42.7.0'
    testImplementation 'junit:junit:4.13.2'
}
"""
            )
        assert db._parse_gradle_build(gradle_path) == {
            "org.slf4j:slf4j-api": "2.0.0",
            "com.google.guava:guava": "33.0.0",
            "jakarta.servlet:jakarta.servlet-api": "6.0.0",
        }
        assert db._parse_kotlin_manifest(gradle_path) == {
            "org.slf4j:slf4j-api": "2.0.0",
            "com.google.guava:guava": "33.0.0",
            "jakarta.servlet:jakarta.servlet-api": "6.0.0",
        }
    finally:
        db.close()


def test_package_lock_uses_root_direct_dependencies_only():
    db, root = _db_with_workspace()
    try:
        lock_path = os.path.join(root, "package-lock.json")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(
                """
{
  "packages": {
    "": {"dependencies": {"react": "^18.0.0"}},
    "node_modules/react": {"version": "18.2.0"},
    "node_modules/loose-envify": {"version": "1.4.0"}
  }
}
"""
            )
        assert db._parse_package_lock(lock_path) == {"react": "18.2.0"}
    finally:
        db.close()


def test_java_archive_scanner_prefers_shallow_public_surface(monkeypatch):
    db, root = _db_with_workspace()
    try:
        jar_path = os.path.join(root, "demo.jar")
        with zipfile.ZipFile(jar_path, "w") as zf:
            zf.writestr("com/example/Public.class", b"")
            zf.writestr("com/example/internal/Hidden.class", b"")
            zf.writestr("com/example/deep/nested/Deep.class", b"")

        calls = []

        class Result:
            returncode = 0
            stdout = "public class com.example.Public {\n  public void run();\n}"

        def fake_run(args, **kwargs):
            if args == ["javap", "-version"]:
                return Result()
            calls.append(args[-1])
            return Result()

        monkeypatch.setattr("callwarden.db.db_external.subprocess.run", fake_run)

        created = db._scan_java_class_jar_via_javap(
            jar_path, "com.example:demo", "1.0.0"
        )

        assert created == 4
        assert calls == ["com.example.Public", "com.example.deep.nested.Deep"]
        rows = [
            r["qualified_name"]
            for r in db.conn.execute(
                "SELECT qualified_name FROM external_symbols ORDER BY qualified_name"
            )
        ]
        assert rows == [
            "com.example.Public",
            "com.example.Public.run",
            "com.example.deep.nested.Deep",
            "com.example.deep.nested.Deep.run",
        ]
    finally:
        db.close()
