// postinstall 脚本：二进制优先，Python 回退
//
// 执行顺序：
// 1. 尝试从 GitHub Releases 下载预编译二进制到 ~/.callwarden/bin/
// 2. 下载失败时回退到检查 Python + pip install callwarden
// 3. 打印清晰的安装结果信息

import { spawnSync, execFileSync } from 'child_process';
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
const CW_BIN_PATHS = [
  path.join(BIN_DIR, CW_BIN_NAME),
  path.join(BIN_DIR, 'cw', CW_BIN_NAME),
];

// ── 工具函数 ──────────────────────────────────────────────────────────

/** 从 package.json 提取 GitHub owner/repo */
function getGitHubRepo() {
  try {
    const pkgPath = path.join(projectRoot, 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
    const url = pkg.repository?.url || '';
    const m = url.match(/github\.com[:/]([^/]+\/[^/.]+)/);
    if (m) return m[1];
  } catch { /* 忽略 */ }
  return 'nuoyazhizhou/callwarden';
}

/** 获取当前平台对应的产物信息 */
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

/** 查找已安装的二进制 */
function findInstalledBinary() {
  for (const p of CW_BIN_PATHS) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

/** https 下载文件（支持 302 重定向） */
function downloadFile(url, destPath) {
  return new Promise((resolve, reject) => {
    const request = (currentUrl, redirectCount = 0) => {
      if (redirectCount > 10) { reject(new Error('重定向次数过多')); return; }
      https.get(currentUrl, { headers: { 'User-Agent': 'callwarden-npm-installer' } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          res.resume();
          request(res.headers.location, redirectCount + 1);
          return;
        }
        if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`HTTP ${res.statusCode}`));
          return;
        }
        const file = createWriteStream(destPath);
        res.pipe(file);
        file.on('finish', () => { file.close(); resolve(); });
        file.on('error', reject);
      }).on('error', reject);
    };
    request(url);
  });
}

/** 下载并安装预编译二进制 */
async function installBinary() {
  const artifact = getPlatformArtifact();
  if (!artifact) return null;

  const repo = getGitHubRepo();
  const url = `https://github.com/${repo}/releases/latest/download/${artifact.filename}`;

  console.log(`📦 正在下载 Call Warden 预编译二进制 (${artifact.platform})...`);

  fs.mkdirSync(BIN_DIR, { recursive: true });
  const tmpArchive = path.join(BIN_DIR, artifact.filename);

  try {
    await downloadFile(url, tmpArchive);

    // 解压归档（使用 execFileSync 避免 shell 注入）
    if (process.platform === 'win32') {
      execFileSync('powershell', [
        '-NoProfile', '-Command',
        `Expand-Archive -LiteralPath '${tmpArchive}' -DestinationPath '${BIN_DIR}' -Force`,
      ], { stdio: 'inherit', timeout: 120000 });
    } else {
      execFileSync('tar', ['xzf', tmpArchive, '-C', BIN_DIR], { stdio: 'inherit', timeout: 120000 });
    }

    // 设置可执行权限（Linux/macOS）
    if (process.platform !== 'win32') {
      for (const p of CW_BIN_PATHS) {
        if (fs.existsSync(p)) fs.chmodSync(p, 0o755);
      }
    }

    return findInstalledBinary();
  } catch (err) {
    console.warn(`   下载失败: ${err.message}`);
    return null;
  } finally {
    try { fs.unlinkSync(tmpArchive); } catch { /* 忽略 */ }
  }
}

// ── Python 回退 ───────────────────────────────────────────────────────

/** 查找系统 Python */
function checkPython() {
  const candidates = ['python3', 'python', 'py'];
  for (const py of candidates) {
    try {
      const result = spawnSync(py, ['-c', 'import sys; print(sys.version_info >= (3, 10))'], {
        encoding: 'utf8', timeout: 5000,
      });
      if (result.status === 0 && result.stdout.trim() === 'True') return py;
    } catch { /* 继续尝试 */ }
  }
  return null;
}

/** 检查 Python 包是否已安装 */
function checkCallWardenPython(python) {
  try {
    const result = spawnSync(python, ['-c', 'import callwarden; print(callwarden.__version__)'], {
      encoding: 'utf8', timeout: 5000,
    });
    return result.status === 0;
  } catch { return false; }
}

/** 通过 pip 安装 Python 包 */
function pipInstall(python) {
  console.log('📦 正在通过 pip 安装 Call Warden Python 包...');
  const result = spawnSync(python, ['-m', 'pip', 'install', 'callwarden'], {
    stdio: 'inherit', timeout: 120000,
  });
  return result.status === 0;
}

// ── 主入口 ────────────────────────────────────────────────────────────

async function main() {
  // 1. 检查是否已安装二进制
  const existing = findInstalledBinary();
  if (existing) {
    console.log(`✅ Call Warden 二进制已就绪: ${existing}`);
    return;
  }

  // 2. 尝试下载预编译二进制
  const installed = await installBinary();
  if (installed) {
    console.log(`✅ Call Warden 二进制安装完成: ${installed}`);
    return;
  }

  // 3. 回退到 Python 路径
  console.log('');
  console.log('⚠️  预编译二进制下载失败，回退到 Python 安装模式...');
  const python = checkPython();

  if (!python) {
    console.warn('⚠️  未找到 Python 3.10+，Call Warden 需要运行环境');
    console.warn('   请安装 Python 3.10+ 后运行: pip install callwarden');
    console.warn('   下载地址: https://www.python.org/downloads/');
    return;
  }

  if (checkCallWardenPython(python)) {
    console.log('✅ Call Warden Python 包已就绪（回退模式）');
    return;
  }

  if (pipInstall(python)) {
    console.log('✅ Call Warden Python 包安装完成（回退模式）');
  } else {
    console.warn('');
    console.warn('⚠️  自动安装失败，请手动运行:');
    console.warn(`   ${python} -m pip install callwarden`);
  }
}

main();
