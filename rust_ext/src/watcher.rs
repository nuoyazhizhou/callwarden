//! Phase 5: File Watcher —— 基于 notify crate 的跨平台文件监听
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §9
//!
//! 核心能力：
//! - 递归监听 workspace root 目录
//! - 按 supported extensions 过滤文件变更事件
//! - 通过 crossbeam channel 传递事件（可选 debounce 在上层实现）
//! - 支持 stop 优雅停止
//!
//! 事件流：
//!   notify crate → raw event → extension filter → channel → Python 消费

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;
use crossbeam_channel::{unbounded, Receiver, Sender, TryRecvError};
use notify::{
    Config, Event, EventHandler, RecommendedWatcher, RecursiveMode, Watcher,
    event::EventKind,
};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

// ============================================
// 支持的文件扩展名（对齐 config.py get_supported_extensions）
// ============================================

/// 默认监听的文件扩展名（16 种语言）
pub fn default_supported_extensions() -> HashSet<String> {
    [
        "rs", "ts", "js", "py", "kt", "go", "java", "c", "cpp", "cc", "h", "hpp",
        "cs", "rb", "php", "swift", "scala", "hcl", "ex", "exs",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect()
}

// ============================================
// FileEvent —— 文件变更事件
// ============================================

/// 文件变更事件类型
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FileEventKind {
    Created,
    Modified,
    Removed,
    Renamed,
}

impl FileEventKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            FileEventKind::Created => "created",
            FileEventKind::Modified => "modified",
            FileEventKind::Removed => "removed",
            FileEventKind::Renamed => "renamed",
        }
    }

    pub fn from_notify_kind(kind: &EventKind) -> Option<Self> {
        match kind {
            EventKind::Create(_) => Some(FileEventKind::Created),
            EventKind::Modify(_) => Some(FileEventKind::Modified),
            EventKind::Remove(_) => Some(FileEventKind::Removed),
            EventKind::Any => Some(FileEventKind::Modified),
            EventKind::Access(_) => None, // 忽略访问事件
            _ => None,
        }
    }
}

/// 文件变更事件
#[derive(Clone, Debug)]
pub struct FileEvent {
    pub kind: FileEventKind,
    pub path: PathBuf,
    pub timestamp_ms: u64,
}

// ============================================
// FileWatcher —— notify crate 封装
// ============================================

/// 文件监听器。
///
/// 内部启动 notify watcher 线程，文件变更事件通过 channel 传递。
/// Python 侧通过 poll_events() 拉取事件。
///
/// Rust 侧用法：
/// ```ignore
/// let watcher = FileWatcher::new("/path/to/workspace", vec!["rs", "py"])?;
/// watcher.start()?;
/// // ... 处理事件
/// let events = watcher.poll_events();
/// watcher.stop();
/// ```
pub struct FileWatcher {
    /// 监听的根目录
    root: PathBuf,
    /// 支持的文件扩展名
    extensions: HashSet<String>,
    /// 事件 channel sender
    tx: Sender<FileEvent>,
    /// 事件 channel receiver
    rx: Receiver<FileEvent>,
    /// notify watcher（start 后填充）
    watcher: parking_lot::Mutex<Option<RecommendedWatcher>>,
    /// 运行标志
    running: Arc<AtomicBool>,
}

impl FileWatcher {
    /// 创建文件监听器
    pub fn new(root: impl AsRef<Path>, extensions: HashSet<String>) -> Self {
        let (tx, rx) = unbounded();
        Self {
            root: root.as_ref().to_path_buf(),
            extensions,
            tx,
            rx,
            watcher: parking_lot::Mutex::new(None),
            running: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 使用默认扩展名创建
    pub fn with_default_extensions(root: impl AsRef<Path>) -> Self {
        Self::new(root, default_supported_extensions())
    }

    /// 启动监听
    pub fn start(&self) -> notify::Result<()> {
        let mut watcher_guard = self.watcher.lock();

        // 如果已在运行，直接返回
        if self.running.load(Ordering::Relaxed) {
            return Ok(());
        }

        // 创建 event handler：将 notify event 转为 FileEvent 并发送到 channel
        let tx = self.tx.clone();
        let extensions = self.extensions.clone();

        let handler = move |res: notify::Result<Event>| {
            match res {
                Ok(event) => {
                    // 从 event kind 转换
                    let kind = match FileEventKind::from_notify_kind(&event.kind) {
                        Some(k) => k,
                        None => return, // 忽略不关心的事件类型
                    };

                    // 过滤路径：只保留匹配扩展名的文件
                    for path in &event.paths {
                        if Self::is_supported(path, &extensions) {
                            let ts = std::time::SystemTime::now()
                                .duration_since(std::time::UNIX_EPOCH)
                                .unwrap_or_default()
                                .as_millis() as u64;
                            let _ = tx.send(FileEvent {
                                kind: kind.clone(),
                                path: path.clone(),
                                timestamp_ms: ts,
                            });
                        }
                    }
                }
                Err(e) => {
                    eprintln!("watch error: {}", e);
                }
            }
        };

        // 创建 RecommendedWatcher
        let mut watcher = RecommendedWatcher::new(
            handler,
            Config::default()
                .with_poll_interval(Duration::from_millis(500))
                .with_compare_contents(false),
        )?;

        // 添加监听路径
        watcher.watch(&self.root, RecursiveMode::Recursive)?;

        *watcher_guard = Some(watcher);
        self.running.store(true, Ordering::Relaxed);

        Ok(())
    }

    /// 停止监听
    pub fn stop(&self) {
        let mut watcher_guard = self.watcher.lock();
        *watcher_guard = None; // drop watcher 停止监听
        self.running.store(false, Ordering::Relaxed);
    }

    /// 是否正在运行
    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::Relaxed)
    }

    /// 拉取所有待处理事件（非阻塞）
    pub fn poll_events(&self) -> Vec<FileEvent> {
        let mut events = Vec::new();
        loop {
            match self.rx.try_recv() {
                Ok(event) => events.push(event),
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => break,
            }
        }
        events
    }

    /// 检查文件是否匹配支持的扩展名
    fn is_supported(path: &Path, extensions: &HashSet<String>) -> bool {
        match path.extension() {
            Some(ext) => {
                let ext_str = ext.to_string_lossy().to_lowercase();
                extensions.contains(&ext_str)
            }
            None => false,
        }
    }

    /// 监听的根目录
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// 支持的扩展名列表
    pub fn extensions(&self) -> Vec<String> {
        let mut exts: Vec<String> = self.extensions.iter().cloned().collect();
        exts.sort();
        exts
    }
}

// ============================================
// DebouncedFileWatcher — debounce + batch coalescing
// ============================================

/// 带 debounce 的文件监听器。
///
/// 收集原始事件并在 debounce 窗口（默认 500ms）内合并：
/// - 同一路径的多次事件合并为单个事件（取最新 kind）
/// - created + modified → created（文件在同一窗口内创建并修改）
/// - modified + removed → removed（文件在同一窗口内修改并删除）
/// - removed + created → modified（文件在同一窗口内删除并重建）
///
/// 设计参考：§9.1 debounce 300-1000ms
pub struct DebouncedFileWatcher {
    /// 内部原始 watcher
    inner: FileWatcher,
    /// debounce 窗口（毫秒）
    debounce_ms: u64,
    /// 上次事件时间戳（用于判断窗口是否结束）
    last_event_ms: parking_lot::Mutex<u64>,
    /// 待处理的事件缓冲（path → 最新 event）
    pending: parking_lot::Mutex<std::collections::HashMap<PathBuf, FileEvent>>,
}

impl DebouncedFileWatcher {
    /// 创建带 debounce 的 watcher
    pub fn new(root: impl AsRef<Path>, extensions: HashSet<String>, debounce_ms: u64) -> Self {
        Self {
            inner: FileWatcher::new(root, extensions),
            debounce_ms,
            last_event_ms: parking_lot::Mutex::new(0),
            pending: parking_lot::Mutex::new(std::collections::HashMap::new()),
        }
    }

    /// 使用默认配置创建
    pub fn with_defaults(root: impl AsRef<Path>) -> Self {
        Self::new(root, default_supported_extensions(), 500)
    }

    /// 启动监听
    pub fn start(&self) -> notify::Result<()> {
        self.inner.start()
    }

    /// 停止监听
    pub fn stop(&self) {
        self.inner.stop();
    }

    /// 是否正在运行
    pub fn is_running(&self) -> bool {
        self.inner.is_running()
    }

    /// 拉取原始事件并加入 pending 缓冲（coalescing）
    ///
    /// last_event_ms 记录最近一次事件的时间戳（event.timestamp_ms），
    /// 而非收集时刻。这样 poll_events 检查 `now - last_event_ms >= debounce_ms`
    /// 时能正确判断窗口是否已过。
    fn collect_raw_events(&self) {
        let raw_events = self.inner.poll_events();
        if raw_events.is_empty() {
            return;
        }

        let mut pending = self.pending.lock();
        let mut last_ts = self.last_event_ms.lock();

        let mut max_ts = *last_ts;
        for event in raw_events {
            // 跟踪最大事件时间戳
            if event.timestamp_ms > max_ts {
                max_ts = event.timestamp_ms;
            }
            // coalescing：同一路径取最新事件，但需考虑事件优先级
            let key = event.path.clone();
            match pending.get(&key) {
                Some(existing) => {
                    let coalesced = coalesce_events(existing, &event);
                    pending.insert(key, coalesced);
                }
                None => {
                    pending.insert(key, event);
                }
            }
        }

        *last_ts = max_ts;
    }

    /// 拉取已过 debounce 窗口的事件（阻塞返回）
    ///
    /// 如果 debounce 窗口未结束，返回空列表。
    /// 如果有新事件但窗口未结束，事件保留在 pending 中。
    pub fn poll_events(&self) -> Vec<FileEvent> {
        self.collect_raw_events();

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;

        let last_ts = *self.last_event_ms.lock();

        // 如果没有 pending 事件，直接返回空
        let pending_len = self.pending.lock().len();
        if pending_len == 0 {
            return vec![];
        }

        // 如果距离最后事件时间不足 debounce 窗口，不返回（等待更多事件）
        if now - last_ts < self.debounce_ms {
            return vec![];
        }

        // debounce 窗口已过，返回所有 pending 事件
        let mut pending = self.pending.lock();
        let events: Vec<FileEvent> = pending.drain().map(|(_, v)| v).collect();
        events
    }

    /// 强制刷新所有 pending 事件（不等待 debounce 窗口）
    pub fn flush(&self) -> Vec<FileEvent> {
        self.collect_raw_events();
        let mut pending = self.pending.lock();
        pending.drain().map(|(_, v)| v).collect()
    }

    /// pending 事件数
    pub fn pending_count(&self) -> usize {
        self.pending.lock().len()
    }

    /// debounce 窗口（毫秒）
    pub fn debounce_ms(&self) -> u64 {
        self.debounce_ms
    }

    /// 监听的根目录
    pub fn root(&self) -> &Path {
        self.inner.root()
    }
}

/// 合并两个同路径事件，返回合并后的事件
///
/// 规则：
/// - created + modified → created（同窗口内创建并修改，视为创建）
/// - modified + removed → removed（同窗口内修改并删除，视为删除）
/// - removed + created → modified（同窗口内删除并重建，视为修改）
/// - 相同 kind → 保留最新时间戳
fn coalesce_events(existing: &FileEvent, new: &FileEvent) -> FileEvent {
    use FileEventKind::*;
    match (&existing.kind, &new.kind) {
        // created + modified → created
        (Created, Modified) => FileEvent {
            kind: Created,
            path: new.path.clone(),
            timestamp_ms: new.timestamp_ms,
        },
        // modified + created → modified（删除后重建）
        (Removed, Created) => FileEvent {
            kind: Modified,
            path: new.path.clone(),
            timestamp_ms: new.timestamp_ms,
        },
        // created + removed → removed（创建后立即删除，视为无变更 → 返回 removed）
        (Created, Removed) => FileEvent {
            kind: Removed,
            path: new.path.clone(),
            timestamp_ms: new.timestamp_ms,
        },
        // 其他情况：取最新事件
        _ => new.clone(),
    }
}

// ============================================
// PyO3 暴露
// ============================================

/// Python 侧文件监听器包装。
///
/// Python 用法：
///   from callwarden_core import PyFileWatcher
///   watcher = PyFileWatcher("/path/to/workspace")
///   watcher.start()
///   events = watcher.poll_events()
///   watcher.stop()
#[pyclass(name = "PyFileWatcher")]
pub struct PyFileWatcher {
    inner: FileWatcher,
}

#[pymethods]
impl PyFileWatcher {
    #[new]
    #[pyo3(signature = (root, extensions=None))]
    fn new(root: &str, extensions: Option<Vec<String>>) -> Self {
        let exts = match extensions {
            Some(exts) => exts.into_iter().collect(),
            None => default_supported_extensions(),
        };
        Self {
            inner: FileWatcher::new(root, exts),
        }
    }

    /// 启动文件监听
    fn start(&self) -> PyResult<()> {
        self.inner.start().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("watcher start failed: {}", e))
        })
    }

    /// 停止文件监听
    fn stop(&self) {
        self.inner.stop();
    }

    /// 是否正在运行
    fn is_running(&self) -> bool {
        self.inner.is_running()
    }

    /// 拉取所有待处理事件（非阻塞）
    fn poll_events<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let events = self.inner.poll_events();
        let list = PyList::empty(py);
        for event in events {
            let d = PyDict::new(py);
            d.set_item("kind", event.kind.as_str())?;
            d.set_item("path", event.path.to_string_lossy().to_string())?;
            d.set_item("timestamp_ms", event.timestamp_ms)?;
            list.append(d)?;
        }
        Ok(list)
    }

    /// 监听的根目录
    fn root(&self) -> String {
        self.inner.root().to_string_lossy().to_string()
    }

    /// 支持的扩展名列表
    fn extensions<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let exts = self.inner.extensions();
        Ok(PyList::new(py, exts.iter().map(String::as_str))?)
    }
}

impl Drop for PyFileWatcher {
    fn drop(&mut self) {
        self.inner.stop();
    }
}

/// Python 侧带 debounce 的文件监听器包装。
///
/// Python 用法：
///   from callwarden_core import PyDebouncedFileWatcher
///   watcher = PyDebouncedFileWatcher("/path/to/workspace", debounce_ms=500)
///   watcher.start()
///   events = watcher.poll_events()  # 返回已过 debounce 窗口的事件
///   events = watcher.flush()  # 强制返回所有 pending
///   watcher.stop()
#[pyclass(name = "PyDebouncedFileWatcher")]
pub struct PyDebouncedFileWatcher {
    inner: DebouncedFileWatcher,
}

#[pymethods]
impl PyDebouncedFileWatcher {
    #[new]
    #[pyo3(signature = (root, extensions=None, debounce_ms=500))]
    fn new(root: &str, extensions: Option<Vec<String>>, debounce_ms: u64) -> Self {
        let exts = match extensions {
            Some(exts) => exts.into_iter().collect(),
            None => default_supported_extensions(),
        };
        Self {
            inner: DebouncedFileWatcher::new(root, exts, debounce_ms),
        }
    }

    /// 启动文件监听
    fn start(&self) -> PyResult<()> {
        self.inner.start().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("watcher start failed: {}", e))
        })
    }

    /// 停止文件监听
    fn stop(&self) {
        self.inner.stop();
    }

    /// 是否正在运行
    fn is_running(&self) -> bool {
        self.inner.is_running()
    }

    /// 拉取已过 debounce 窗口的事件（非阻塞）
    ///
    /// 如果 debounce 窗口未结束，返回空列表。
    fn poll_events<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let events = self.inner.poll_events();
        let list = PyList::empty(py);
        for event in events {
            let d = PyDict::new(py);
            d.set_item("kind", event.kind.as_str())?;
            d.set_item("path", event.path.to_string_lossy().to_string())?;
            d.set_item("timestamp_ms", event.timestamp_ms)?;
            list.append(d)?;
        }
        Ok(list)
    }

    /// 强制刷新所有 pending 事件（不等待 debounce 窗口）
    fn flush<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let events = self.inner.flush();
        let list = PyList::empty(py);
        for event in events {
            let d = PyDict::new(py);
            d.set_item("kind", event.kind.as_str())?;
            d.set_item("path", event.path.to_string_lossy().to_string())?;
            d.set_item("timestamp_ms", event.timestamp_ms)?;
            list.append(d)?;
        }
        Ok(list)
    }

    /// 当前 pending 事件数
    fn pending_count(&self) -> usize {
        self.inner.pending_count()
    }

    /// debounce 窗口（毫秒）
    fn debounce_ms(&self) -> u64 {
        self.inner.debounce_ms()
    }

    /// 监听的根目录
    fn root(&self) -> String {
        self.inner.root().to_string_lossy().to_string()
    }
}

impl Drop for PyDebouncedFileWatcher {
    fn drop(&mut self) {
        self.inner.stop();
    }
}
