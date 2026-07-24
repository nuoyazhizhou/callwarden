#!/usr/bin/env node
// Call Warden 二进制委托器
// 优先使用 ~/.callwarden/bin/ 下的预编译二进制，
// 若不存在则从 GitHub Releases 自动下载，
// 下载失败则回退到本地 Python + cw.py 路径。

import { spawn, spawnSync, execFileSync } from 'child_process';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';
import os from 'os';
import https from 'https';
import { createWriteStream } from 'fs';

// ── 路径常量 ──────────────────────────────────────────────────────────
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, '..');

const CALLWARDEN_HOME = path.join(os.homedir(), '.callwarden');
const BIN_DIR = path.join(CALLWARDEN_HOME, 'bin');
const CW_BIN_NAME = process.platform === 'win32' ? 'cw.exe' : 'cw';

// 解压后二进制可能在子目录 cw/ 下（CI 打包的是目录而非单文件）
const CW_BIN_PATHS = [
  path.join(BIN_DIR, CW_BIN_NAME),
  path.join(BIN_DIR, 'cw', CW_BIN_NAME),
];

// ── 工具函数 ──────────────────────────────────────────────────────────

/** 从 package.json 的 repository.url 提取 GitHub owner/repo */
function getGitHubRepo() {
  try {
    const pkgPath = path.join(projectRoot, 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
    const url = pkg.repository?.url || '';
    // 匹配 https://github.com/owner/repo.git 或 git@github.com:owner/repo.git
    const m = url.match(/github\.com[:/]([^/]+\/[^/.]+)/);
    if (m) return m[1];
  } catch { /* 忽略 */ }
  // 兜底默认值
  return 'nuoyazhizhou/callwarden';
}

/** 获取当前平台对应的 CI 产物信息 */
function getPlatformArtifact() {
  const arch = process.arch === 'arm64' ? 'arm64' : 'amd64';
  const platformMap = {
    'win32': `windows-${arch}`,
    'linux': `linux-${arch}`,
    'darwin': `macos-${process.arch === 'arm64' ? 'arm64' : 'amd64'}`,
  };
  const platform = platformMap[process.platform];
  if (!platform) return null;

  const ext = process.platform === 'win32' ? 'zip' : 'tar.gz';
  return { platform, ext, filename: `callwarden-${platform}.${ext}` };
}

/** 查找已安装的二进制路径 */
function findInstalledBinary() {
  for (const p of CW_BIN_PATHS) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

/** 通过 https 下载文件，支持 302 重定向（GitHub Releases 会重定向到 CDN） */
function downloadFile(url, destPath) {
  return new Promise((resolve, reject) => {
    const request = (currentUrl, redirectCount = 0) => {
      if (redirectCount > 10) {
        reject(new Error('重定向次数过多'));
        return;
      }
      https.get(currentUrl, { headers: { 'User-Agent': 'callwarden-npm-installer' } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          // 消费响应体避免内存泄漏
          res.resume();
          request(res.headers.location, redirectCount + 1);
          return;
        }
        if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`HTTP ${res.statusCode}`));
          return;
        }
        const contentLength = parseInt(res.headers['content-length'] || '0', 10);
        let downloaded = 0;
        let lastPercent = -1;

        const file = createWriteStream(destPath);
        res.on('data', (chunk) => {
          downloaded += chunk.length;
          if (contentLength > 0) {
            const pct = Math.floor((downloaded / contentLength) * 100);
            // 每 10% 打印一次进度
            if (Math.floor(pct / 10) > Math.floor(lastPercent / 10)) {
              const mb = (downloaded / 1048576).toFixed(1);
              const total = (contentLength / 1048576).toFixed(1);
              process.stderr.write(`\r   下载进度: ${pct}% (${mb}/${total} MB)`);
              lastPercent = pct;
            }
          }
        });
        res.pipe(file);
        file.on('finish', () => {
          if (contentLength > 0) process.stderr.write('\n');
          file.close();
          resolve();
        });
        file.on('error', reject);
      }).on('error', reject);
    };
    request(url);
  });
}

/** 解压归档到目标目录（使用 execFileSync 避免 shell 注入） */
function extractArchive(archivePath, destDir) {
  if (process.platform === 'win32') {
    // Windows：使用 PowerShell 解压 zip（-Force 覆盖已有文件）
    execFileSync('powershell', [
      '-NoProfile', '-Command',
      `Expand-Archive -LiteralPath '${archivePath}' -DestinationPath '${destDir}' -Force`,
    ], { stdio: 'inherit', timeout: 120000 });
  } else {
    // Linux/macOS：使用 tar 解压
    execFileSync('tar', ['xzf', archivePath, '-C', destDir], { stdio: 'inherit', timeout: 120000 });
  }
}

/** 从 GitHub Releases 下载并安装预编译二进制 */
async function installCwBinary() {
  const artifact = getPlatformArtifact();
  if (!artifact) {
    console.error(`[callwarden] 不支持的平台: ${process.platform}-${process.arch}`);
    return null;
  }

  const repo = getGitHubRepo();
  const releaseUrl = `https://github.com/${repo}/releases/latest/download/${artifact.filename}`;

  console.log(`📦 正在下载 Call Warden 预编译二进制 (${artifact.platform})...`);
  console.log(`   ${releaseUrl}`);

  fs.mkdirSync(BIN_DIR, { recursive: true });

  const tmpArchive = path.join(BIN_DIR, artifact.filename);
  try {
    await downloadFile(releaseUrl, tmpArchive);
    await extractArchive(tmpArchive, BIN_DIR);

    // 设置可执行权限（Linux/macOS）
    if (process.platform !== 'win32') {
      for (const p of CW_BIN_PATHS) {
        if (fs.existsSync(p)) {
          fs.chmodSync(p, 0o755);
        }
      }
    }

    const installed = findInstalledBinary();
    if (installed) {
      console.log('✅ Call Warden 二进制安装完成');
      return installed;
    }
    console.error('[callwarden] 解压后未找到可执行文件');
    return null;
  } catch (err) {
    console.error(`[callwarden] 下载失败: ${err.message}`);
    return null;
  } finally {
    // 清理临时归档文件
    try { fs.unlinkSync(tmpArchive); } catch { /* 忽略 */ }
  }
}

// ── Python 回退路径 ──────────────────────────────────────────────────

/** 查找系统 Python 解释器 */
function findPython() {
  const candidates = ['python3', 'python', 'py'];
  for (const py of candidates) {
    try {
      const result = spawnSync(py, ['--version'], { encoding: 'utf8', timeout: 5000 });
      if (result.status === 0) return py;
    } catch { /* 继续尝试下一个 */ }
  }
  return null;
}

/** 回退到 Python 方式执行 cw.py */
function runViaPython(python, args) {
  const cwPy = path.join(projectRoot, 'cw.py');
  const cmd = fs.existsSync(cwPy) ? [cwPy, ...args] : ['-m', 'callwarden', ...args];

  const child = spawn(python, cmd, { stdio: 'inherit', env: { ...process.env } });
  child.on('exit', (code) => process.exit(code || 0));
  child.on('error', (err) => {
    console.error(`执行 Python 失败: ${err.message}`);
    process.exit(1);
  });
}

// ── 主入口 ────────────────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);

  // 1. 检查是否已有预编译二进制
  let cwBin = findInstalledBinary();

  if (!cwBin) {
    // 2. 尝试从 GitHub Releases 下载
    cwBin = await installCwBinary();
  }

  if (cwBin) {
    // 3a. 使用预编译二进制委托执行
    const child = spawn(cwBin, args, { stdio: 'inherit' });
    child.on('exit', (code) => process.exit(code || 0));
    child.on('error', (err) => {
      console.error(`执行二进制失败: ${err.message}`);
      // 回退到 Python
      const python = findPython();
      if (python) {
        console.log('[callwarden] 回退到 Python 模式...');
        runViaPython(python, args);
      } else {
        process.exit(1);
      }
    });
    return;
  }

  // 3b. 二进制不可用，回退到 Python 路径
  console.log('[callwarden] 预编译二进制不可用，回退到 Python 模式...');
  const python = findPython();
  if (!python) {
    console.error('错误：未找到预编译二进制或 Python 3 解释器。');
    console.error('请安装 Python 3.10+，或等待 GitHub Release 发布后重试。');
    process.exit(1);
  }
  runViaPython(python, args);
}

main();
