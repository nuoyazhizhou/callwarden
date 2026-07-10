//! Phase 5.2: Changed File Hash Diff
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §9.1
//!
//! 文件保存流程：
//!   file event → debounce → compute content hash → compare with last known
//!     - unchanged → drop（watcher 假阳性，如 touch 不改内容）
//!     - changed → CAS lookup / parse worker
//!
//! 本模块负责：
//! - 计算文件内容 SHA-256 hash
//! - 维护 path → content_hash 映射
//! - 对一批 FileEvent 做 diff，返回真正发生内容变更的文件列表

use std::collections::HashMap;
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sha2::{Digest, Sha256};

use crate::watcher::{FileEvent, FileEventKind};

// ============================================
// FileChange —— 真正发生内容变更的文件
// ============================================

/// 文件变更类型（比 FileEventKind 更语义化）
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FileChangeKind {
    /// 新文件（之前无 hash 记录）
    Added,
    /// 文件内容修改
    Modified,
    /// 文件删除
    Removed,
}

impl FileChangeKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            FileChangeKind::Added => "added",
            FileChangeKind::Modified => "modified",
            FileChangeKind::Removed => "removed",
        }
    }
}

/// 真正发生内容变更的文件记录
#[derive(Clone, Debug)]
pub struct FileChange {
    pub kind: FileChangeKind,
    pub path: PathBuf,
    /// 当前内容 hash（Removed 时为 None）
    pub content_hash: Option<String>,
    /// 之前的 hash（Added 时为 None）
    pub previous_hash: Option<String>,
}

impl FileChange {
    pub fn is_added(&self) -> bool {
        matches!(self.kind, FileChangeKind::Added)
    }

    pub fn is_modified(&self) -> bool {
        matches!(self.kind, FileChangeKind::Modified)
    }

    pub fn is_removed(&self) -> bool {
        matches!(self.kind, FileChangeKind::Removed)
    }
}

// ============================================
// HashDiffStore —— path → content_hash 映射
// ============================================

/// 文件内容 hash 存储，用于检测文件是否真正发生变化。
///
/// 工作流程：
/// 1. watcher 产生 FileEvent（可能包含假阳性，如 touch 不改内容）
/// 2. 调用 `diff_events(events)` 对比当前文件 hash 与存储的 hash
/// 3. 返回真正发生内容变更的 FileChange 列表
/// 4. 内部自动更新 hash 存储
///
/// 线程安全：内部用 parking_lot::Mutex 保护 HashMap。
pub struct HashDiffStore {
    /// path → content_hash
    hashes: parking_lot::Mutex<HashMap<PathBuf, String>>,
}

impl HashDiffStore {
    /// 创建空的 hash 存储
    pub fn new() -> Self {
        Self {
            hashes: parking_lot::Mutex::new(HashMap::new()),
        }
    }

    /// 从已有映射创建（用于从 manifest 恢复）
    pub fn with_hashes(hashes: HashMap<PathBuf, String>) -> Self {
        Self {
            hashes: parking_lot::Mutex::new(hashes),
        }
    }

    /// 计算文件内容的 SHA-256 hash
    ///
    /// 返回 (hash_hex, content_size)
    /// 文件不存在或读取失败时返回 (None, 0)
    pub fn compute_file_hash(path: &Path) -> Option<(String, u64)> {
        let mut file = match fs::File::open(path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return None,
            Err(_) => return None,
        };

        let mut hasher = Sha256::new();
        let mut buf = [0u8; 8192];
        let mut total_size: u64 = 0;

        loop {
            match file.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    hasher.update(&buf[..n]);
                    total_size += n as u64;
                }
                Err(_) => return None,
            }
        }

        let hash = hasher.finalize();
        Some((format!("{:x}", hash), total_size))
    }

    /// 对一批 FileEvent 做 hash diff，返回真正发生内容变更的文件列表。
    ///
    /// 处理逻辑：
    /// - **Created 事件**：计算当前 hash。若之前无记录 → Added；若与之前相同 → 忽略（假创建）；若不同 → Modified
    /// - **Modified 事件**：计算当前 hash。若与之前相同 → 忽略（假修改）；若不同 → Modified；若之前无记录 → Added
    /// - **Removed 事件**：若之前有记录 → Removed；若之前无记录 → 忽略
    /// - **Renamed 事件**：按 Modified 处理（路径已变化）
    ///
    /// 内部会更新 hash 存储。
    pub fn diff_events(&self, events: &[FileEvent]) -> Vec<FileChange> {
        let mut changes = Vec::new();
        let mut hashes = self.hashes.lock();

        for event in events {
            let path = &event.path;
            let prev_hash = hashes.get(path).cloned();

            match event.kind {
                FileEventKind::Created | FileEventKind::Renamed => {
                    match Self::compute_file_hash(path) {
                        Some((curr_hash, _size)) => {
                            if prev_hash.as_ref() == Some(&curr_hash) {
                                // 内容未变（假创建），忽略
                                continue;
                            }
                            let change_kind = if prev_hash.is_none() {
                                FileChangeKind::Added
                            } else {
                                FileChangeKind::Modified
                            };
                            hashes.insert(path.clone(), curr_hash.clone());
                            changes.push(FileChange {
                                kind: change_kind,
                                path: path.clone(),
                                content_hash: Some(curr_hash),
                                previous_hash: prev_hash,
                            });
                        }
                        None => {
                            // 文件已不存在（可能在 debounce 窗口内又被删除）
                            // 如果之前有记录，标记为 Removed
                            if let Some(prev) = prev_hash {
                                hashes.remove(path);
                                changes.push(FileChange {
                                    kind: FileChangeKind::Removed,
                                    path: path.clone(),
                                    content_hash: None,
                                    previous_hash: Some(prev),
                                });
                            }
                        }
                    }
                }
                FileEventKind::Modified => {
                    match Self::compute_file_hash(path) {
                        Some((curr_hash, _size)) => {
                            if prev_hash.as_ref() == Some(&curr_hash) {
                                // 内容未变（假修改，如 touch），忽略
                                continue;
                            }
                            let change_kind = if prev_hash.is_none() {
                                FileChangeKind::Added
                            } else {
                                FileChangeKind::Modified
                            };
                            hashes.insert(path.clone(), curr_hash.clone());
                            changes.push(FileChange {
                                kind: change_kind,
                                path: path.clone(),
                                content_hash: Some(curr_hash),
                                previous_hash: prev_hash,
                            });
                        }
                        None => {
                            // 文件已不存在
                            if let Some(prev) = prev_hash {
                                hashes.remove(path);
                                changes.push(FileChange {
                                    kind: FileChangeKind::Removed,
                                    path: path.clone(),
                                    content_hash: None,
                                    previous_hash: Some(prev),
                                });
                            }
                        }
                    }
                }
                FileEventKind::Removed => {
                    if let Some(prev) = prev_hash {
                        hashes.remove(path);
                        changes.push(FileChange {
                            kind: FileChangeKind::Removed,
                            path: path.clone(),
                            content_hash: None,
                            previous_hash: Some(prev),
                        });
                    }
                    // 之前无记录 → 忽略（可能从未索引过的文件被删除）
                }
            }
        }

        changes
    }

    /// 手动注册文件 hash（用于初始化时批量加载已有文件索引）
    pub fn register_hash(&self, path: PathBuf, hash: String) {
        self.hashes.lock().insert(path, hash);
    }

    /// 批量注册 hash
    pub fn register_hashes(&self, entries: HashMap<PathBuf, String>) {
        let mut hashes = self.hashes.lock();
        for (path, hash) in entries {
            hashes.insert(path, hash);
        }
    }

    /// 获取文件的当前 hash
    pub fn get_hash(&self, path: &Path) -> Option<String> {
        self.hashes.lock().get(path).cloned()
    }

    /// 当前已跟踪的文件数
    pub fn tracked_count(&self) -> usize {
        self.hashes.lock().len()
    }

    /// 获取所有已跟踪文件的 hash 快照（用于 manifest 持久化）
    pub fn snapshot(&self) -> HashMap<PathBuf, String> {
        self.hashes.lock().clone()
    }

    /// 清空所有 hash 记录
    pub fn clear(&self) {
        self.hashes.lock().clear();
    }
}

impl Default for HashDiffStore {
    fn default() -> Self {
        Self::new()
    }
}

// ============================================
// PyO3 暴露
// ============================================

/// Python 侧 hash diff 存储包装。
///
/// Python 用法：
///   from callwarden_core import PyHashDiffStore
///   store = PyHashDiffStore()
///   store.register_hash("/path/to/file.py", "abc123...")
///   changes = store.diff_events(events_from_watcher)
///   for c in changes:
///       print(c["kind"], c["path"], c["content_hash"])
#[pyclass(name = "PyHashDiffStore")]
pub struct PyHashDiffStore {
    inner: HashDiffStore,
}

#[pymethods]
impl PyHashDiffStore {
    #[new]
    fn new() -> Self {
        Self {
            inner: HashDiffStore::new(),
        }
    }

    /// 对一批 watcher 事件做 hash diff，返回真正变更的文件列表
    ///
    /// 参数 events 是 list of (kind, path, timestamp_ms) 元组
    /// 例如 [("modified", "/path/to/file.py", 1234567890), ...]
    fn diff_events<'py>(
        &self,
        py: Python<'py>,
        events: Vec<(String, String, u64)>,
    ) -> PyResult<Bound<'py, PyList>> {
        let mut file_events = Vec::new();
        for (kind_str, path_str, ts) in events {
            let kind = match kind_str.as_str() {
                "created" => FileEventKind::Created,
                "modified" => FileEventKind::Modified,
                "removed" => FileEventKind::Removed,
                "renamed" => FileEventKind::Renamed,
                _ => {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!(
                        "unknown event kind: {}",
                        kind_str
                    )))
                }
            };

            file_events.push(FileEvent {
                kind,
                path: PathBuf::from(&path_str),
                timestamp_ms: ts,
            });
        }

        let changes = self.inner.diff_events(&file_events);
        let result = PyList::empty(py);
        for change in changes {
            let d = PyDict::new(py);
            d.set_item("kind", change.kind.as_str())?;
            d.set_item("path", change.path.to_string_lossy().to_string())?;
            d.set_item("content_hash", change.content_hash)?;
            d.set_item("previous_hash", change.previous_hash)?;
            result.append(d)?;
        }
        Ok(result)
    }

    /// 手动注册文件 hash（用于初始化）
    fn register_hash(&self, path: &str, hash: &str) {
        self.inner.register_hash(PathBuf::from(path), hash.to_string());
    }

    /// 获取文件的当前 hash
    fn get_hash(&self, path: &str) -> Option<String> {
        self.inner.get_hash(Path::new(path))
    }

    /// 当前已跟踪的文件数
    fn tracked_count(&self) -> usize {
        self.inner.tracked_count()
    }

    /// 清空所有 hash 记录
    fn clear(&self) {
        self.inner.clear();
    }

    /// 获取所有已跟踪文件的 hash 快照
    /// 返回 list of (path, hash) tuples
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let snap = self.inner.snapshot();
        let result = PyList::empty(py);
        for (path, hash) in snap {
            let t = (path.to_string_lossy().to_string(), hash);
            result.append(t)?;
        }
        Ok(result)
    }

    /// __repr__
    fn __repr__(&self) -> String {
        format!("PyHashDiffStore(tracked={})", self.inner.tracked_count())
    }
}

// ============================================
// 测试辅助函数
// ============================================

/// 计算文件 SHA-256 hash 的独立函数（不依赖 HashDiffStore）
#[allow(dead_code)]
pub fn compute_file_hash_standalone(path: &Path) -> Option<String> {
    HashDiffStore::compute_file_hash(path).map(|(h, _)| h)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn test_compute_file_hash() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.py");
        std::fs::write(&path, "print('hello')\n").unwrap();

        let (hash, size) = HashDiffStore::compute_file_hash(&path).unwrap();
        assert_eq!(size, 15);
        assert_eq!(hash.len(), 64); // SHA-256 hex = 64 chars
    }

    #[test]
    fn test_hash_is_deterministic() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.py");
        std::fs::write(&path, "x = 1\n").unwrap();

        let (h1, _) = HashDiffStore::compute_file_hash(&path).unwrap();
        let (h2, _) = HashDiffStore::compute_file_hash(&path).unwrap();
        assert_eq!(h1, h2);
    }

    #[test]
    fn test_hash_differs_for_different_content() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.py");

        std::fs::write(&path, "x = 1\n").unwrap();
        let (h1, _) = HashDiffStore::compute_file_hash(&path).unwrap();

        std::fs::write(&path, "x = 2\n").unwrap();
        let (h2, _) = HashDiffStore::compute_file_hash(&path).unwrap();

        assert_ne!(h1, h2);
    }

    #[test]
    fn test_hash_nonexistent_file() {
        let path = Path::new("/nonexistent/path/file.py");
        assert!(HashDiffStore::compute_file_hash(path).is_none());
    }

    #[test]
    fn test_diff_events_added() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("new.py");
        std::fs::write(&path, "x = 1\n").unwrap();

        let store = HashDiffStore::new();
        let events = vec![FileEvent {
            kind: FileEventKind::Created,
            path: path.clone(),
            timestamp_ms: 1000,
        }];

        let changes = store.diff_events(&events);
        assert_eq!(changes.len(), 1);
        assert!(changes[0].is_added());
        assert!(changes[0].content_hash.is_some());
        assert!(changes[0].previous_hash.is_none());
    }

    #[test]
    fn test_diff_events_unchanged_dropped() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("same.py");
        std::fs::write(&path, "x = 1\n").unwrap();

        let store = HashDiffStore::new();

        // 第一次 Modified 事件 → Added
        let events1 = vec![FileEvent {
            kind: FileEventKind::Modified,
            path: path.clone(),
            timestamp_ms: 1000,
        }];
        let changes1 = store.diff_events(&events1);
        assert_eq!(changes1.len(), 1);

        // 第二次 Modified 事件，内容未变 → 忽略
        let events2 = vec![FileEvent {
            kind: FileEventKind::Modified,
            path: path.clone(),
            timestamp_ms: 2000,
        }];
        let changes2 = store.diff_events(&events2);
        assert_eq!(changes2.len(), 0); // 假阳性被过滤
    }

    #[test]
    fn test_diff_events_removed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("del.py");
        std::fs::write(&path, "x = 1\n").unwrap();

        let store = HashDiffStore::new();

        // 先注册
        let events1 = vec![FileEvent {
            kind: FileEventKind::Created,
            path: path.clone(),
            timestamp_ms: 1000,
        }];
        store.diff_events(&events1);

        // 删除文件
        std::fs::remove_file(&path).unwrap();
        let events2 = vec![FileEvent {
            kind: FileEventKind::Removed,
            path: path.clone(),
            timestamp_ms: 2000,
        }];
        let changes2 = store.diff_events(&events2);
        assert_eq!(changes2.len(), 1);
        assert!(changes2[0].is_removed());
        assert!(changes2[0].content_hash.is_none());
        assert!(changes2[0].previous_hash.is_some());
    }

    #[test]
    fn test_diff_events_modified() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("mod.py");

        // 初始内容
        std::fs::write(&path, "x = 1\n").unwrap();
        let store = HashDiffStore::new();
        let events1 = vec![FileEvent {
            kind: FileEventKind::Created,
            path: path.clone(),
            timestamp_ms: 1000,
        }];
        store.diff_events(&events1);

        // 修改内容
        std::fs::write(&path, "y = 2\n").unwrap();
        let events2 = vec![FileEvent {
            kind: FileEventKind::Modified,
            path: path.clone(),
            timestamp_ms: 2000,
        }];
        let changes2 = store.diff_events(&events2);
        assert_eq!(changes2.len(), 1);
        assert!(changes2[0].is_modified());
        assert!(changes2[0].content_hash.is_some());
        assert!(changes2[0].previous_hash.is_some());
        assert_ne!(
            changes2[0].content_hash.as_ref().unwrap(),
            changes2[0].previous_hash.as_ref().unwrap()
        );
    }

    #[test]
    fn test_diff_events_multiple() {
        let dir = tempfile::tempdir().unwrap();

        // 文件 A：新增
        let path_a = dir.path().join("a.py");
        std::fs::write(&path_a, "a = 1\n").unwrap();

        // 文件 B：新增后修改
        let path_b = dir.path().join("b.py");
        std::fs::write(&path_b, "b = 1\n").unwrap();

        let store = HashDiffStore::new();

        // 批量注册
        let events1 = vec![
            FileEvent {
                kind: FileEventKind::Created,
                path: path_a.clone(),
                timestamp_ms: 1000,
            },
            FileEvent {
                kind: FileEventKind::Created,
                path: path_b.clone(),
                timestamp_ms: 1000,
            },
        ];
        let changes1 = store.diff_events(&events1);
        assert_eq!(changes1.len(), 2);
        assert_eq!(store.tracked_count(), 2);

        // 修改 B，不触碰 A
        std::fs::write(&path_b, "b = 2\n").unwrap();
        let events2 = vec![FileEvent {
            kind: FileEventKind::Modified,
            path: path_b.clone(),
            timestamp_ms: 2000,
        }];
        let changes2 = store.diff_events(&events2);
        assert_eq!(changes2.len(), 1);
        assert!(changes2[0].is_modified());
    }

    #[test]
    fn test_register_and_get_hash() {
        let store = HashDiffStore::new();
        store.register_hash(PathBuf::from("/test/file.py"), "abc123".to_string());
        assert_eq!(
            store.get_hash(Path::new("/test/file.py")),
            Some("abc123".to_string())
        );
        assert_eq!(store.tracked_count(), 1);
    }

    #[test]
    fn test_clear() {
        let store = HashDiffStore::new();
        store.register_hash(PathBuf::from("/test/file.py"), "abc".to_string());
        assert_eq!(store.tracked_count(), 1);
        store.clear();
        assert_eq!(store.tracked_count(), 0);
    }

    #[test]
    fn test_snapshot() {
        let store = HashDiffStore::new();
        store.register_hash(PathBuf::from("/a.py"), "hash_a".to_string());
        store.register_hash(PathBuf::from("/b.py"), "hash_b".to_string());

        let snap = store.snapshot();
        assert_eq!(snap.len(), 2);
        assert_eq!(snap.get(Path::new("/a.py")), Some(&"hash_a".to_string()));
    }
}
