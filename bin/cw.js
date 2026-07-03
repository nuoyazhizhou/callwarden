#!/usr/bin/env node
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, '..');

function findPython() {
  const pythonCandidates = ['python3', 'python', 'py'];
  for (const py of pythonCandidates) {
    try {
      const result = spawn.sync(py, ['--version'], { encoding: 'utf8' });
      if (result.status === 0) return py;
    } catch (e) {
      // 继续尝试下一个
    }
  }
  return null;
}

function main() {
  const python = findPython();
  
  if (!python) {
    console.error('错误：未找到 Python 3 解释器。');
    console.error('请先安装 Python 3.9+，然后运行: pip install callwarden');
    console.error('');
    console.error('或者使用 pip 直接安装 Call Warden：');
    console.error('  pip install callwarden');
    process.exit(1);
  }

  const args = process.argv.slice(2);
  
  const cwPy = path.join(projectRoot, 'cw.py');
  
  if (fs.existsSync(cwPy)) {
    const child = spawn(python, [cwPy, ...args], {
      stdio: 'inherit',
      env: { ...process.env }
    });
    
    child.on('exit', (code) => {
      process.exit(code || 0);
    });
    
    child.on('error', (err) => {
      console.error(`执行 cw.py 失败: ${err.message}`);
      process.exit(1);
    });
  } else {
    const child = spawn(python, ['-m', 'callwarden', ...args], {
      stdio: 'inherit',
      env: { ...process.env }
    });
    
    child.on('exit', (code) => {
      process.exit(code || 0);
    });
    
    child.on('error', (err) => {
      console.error(`执行 python -m callwarden 失败: ${err.message}`);
      console.error('请确保已通过 pip install callwarden 安装 Python 包');
      process.exit(1);
    });
  }
}

main();
