//! UDS server——Unix Domain Socket 监听 + 连接接受。
//! 仅 Linux/Unix 可用，Windows 上跳过编译。

use std::os::unix::net::UnixListener;
use std::path::Path;
use super::DaemonConfig;

pub fn start_server(config: &DaemonConfig) -> std::io::Result<()> {
    let socket_path = Path::new(&config.socket_path);
    
    // 清理旧 socket 文件
    if socket_path.exists() {
        std::fs::remove_file(socket_path)?;
    }
    
    let listener = UnixListener::bind(socket_path)?;
    
    // 设置权限 0660
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
    
    eprintln!("[cw_daemon] listening on {}", config.socket_path);
    
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                if let Err(e) = super::peercred::handle_connection(&stream) {
                    eprintln!("[cw_daemon] connection error: {}", e);
                }
            }
            Err(e) => {
                eprintln!("[cw_daemon] accept error: {}", e);
            }
        }
    }
    Ok(())
}
