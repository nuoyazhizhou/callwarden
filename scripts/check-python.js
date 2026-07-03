import { spawnSync } from 'child_process';

function checkPython() {
  const candidates = ['python3', 'python', 'py'];
  for (const py of candidates) {
    try {
      const result = spawnSync(py, ['-c', 'import sys; print(sys.version_info >= (3, 9))'], {
        encoding: 'utf8',
        timeout: 5000
      });
      if (result.status === 0 && result.stdout.trim() === 'True') {
        return py;
      }
    } catch (e) {
      // 继续尝试
    }
  }
  return null;
}

function checkCallWarden(python) {
  try {
    const result = spawnSync(python, ['-c', 'import callwarden; print(callwarden.__version__)'], {
      encoding: 'utf8',
      timeout: 5000
    });
    return result.status === 0;
  } catch (e) {
    return false;
  }
}

function main() {
  const python = checkPython();
  
  if (!python) {
    console.warn('⚠️  未找到 Python 3.9+，Call Warden 需要 Python 环境');
    console.warn('   请安装 Python 3.9+ 后运行: pip install callwarden');
    console.warn('');
    console.warn('   下载地址: https://www.python.org/downloads/');
    return;
  }
  
  if (checkCallWarden(python)) {
    console.log('✅ Call Warden Python 包已就绪');
    return;
  }
  
  console.log('📦 正在安装 Call Warden Python 包...');
  const result = spawnSync(python, ['-m', 'pip', 'install', 'callwarden'], {
    stdio: 'inherit',
    timeout: 120000
  });
  
  if (result.status !== 0) {
    console.warn('');
    console.warn('⚠️  自动安装失败，请手动运行:');
    console.warn(`   ${python} -m pip install callwarden`);
  } else {
    console.log('✅ Call Warden 安装完成！');
  }
}

main();
