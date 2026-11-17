//! Spawns the Next.js `standalone` server as a plain OS process (no bundled
//! Node runtime -- see design-discussion.md §2: this app targets one
//! already-provisioned machine, not portable distribution) and waits for it
//! to answer /api/health?shallow=1 before the window is allowed to point at it.
//!
//! Two real risks this module exists to handle explicitly (research-brief.md
//! §3): a GUI-launched process on macOS gets a near-empty PATH, so the
//! sidecar's own `spawn("portunus", ...)` calls would silently fail to find
//! the pip-installed CLI unless we capture and forward the *real* login-shell
//! PATH; and a hardcoded port can collide with something else already
//! running (it did, during this session's own testing), so we always bind a
//! fresh OS-assigned free port.

use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::net::TcpListener;
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use tauri::{AppHandle, Manager};
use wait_timeout::ChildExt;

const PATH_CAPTURE_TIMEOUT: Duration = Duration::from_secs(5);
const HEALTH_POLL_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(200);

/// A conservative fallback PATH used only if capturing the user's real login
/// shell PATH fails outright (unusual shell config, timeout) -- covers the
/// common install locations for both the pip-installed `portunus` CLI and
/// Homebrew's `node`, so the app degrades rather than hanging forever.
fn fallback_path() -> String {
    let home = env::var("HOME").unwrap_or_default();
    format!(
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:{home}/.local/bin:{home}/Library/Python/3.13/bin"
    )
}

/// Runs the user's own login shell non-interactively to capture the *real*
/// PATH (GUI-launched apps on macOS do not source .zshrc/.zprofile, so
/// `std::env::var("PATH")` inside a Tauri app is near-empty -- a confirmed,
/// not hypothetical, gotcha). Bounded by a timeout so a hung/unusual shell
/// config can never block app launch indefinitely.
pub fn capture_login_shell_path() -> String {
    let shell = env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
    let mut child = match Command::new(&shell)
        .args(["-ilc", "echo -n \"$PATH\""])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => return fallback_path(),
    };

    match child.wait_timeout(PATH_CAPTURE_TIMEOUT) {
        Ok(Some(status)) if status.success() => {
            let mut out = String::new();
            if let Some(mut stdout) = child.stdout.take() {
                let _ = stdout.read_to_string(&mut out);
            }
            let out = out.trim().to_string();
            if out.is_empty() {
                fallback_path()
            } else {
                out
            }
        }
        Ok(Some(_)) => fallback_path(),
        Ok(None) => {
            // Timed out -- kill it and fall back rather than wait forever.
            let _ = child.kill();
            let _ = child.wait();
            fallback_path()
        }
        Err(_) => fallback_path(),
    }
}

/// Binds port 0 to get a free OS-assigned port, then immediately releases it.
/// A small TOCTOU race exists between release and the sidecar's own bind --
/// acceptable for a single-user local app (design-discussion.md §7).
pub fn pick_free_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").expect("failed to bind an ephemeral port");
    listener.local_addr().expect("listener has no local addr").port()
}

/// Resolves the Next.js standalone `server.js` this app should spawn.
/// Prefers the bundled Tauri resource (the real installed-app path); falls
/// back to the live `ui/.next/standalone` build for `cargo tauri dev`
/// iteration, where resources aren't copied into a bundle at all.
pub fn resolve_server_js(app: &AppHandle) -> PathBuf {
    if let Ok(resource_dir) = app.path().resource_dir() {
        let bundled = resource_dir.join("web").join("server.js");
        if bundled.exists() {
            return bundled;
        }
    }
    // Dev fallback only -- CARGO_MANIFEST_DIR is ui/src-tauri, so ../.next.
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    manifest_dir.join("..").join(".next").join("standalone").join("server.js")
}

pub struct SidecarHandle {
    pub child: Child,
    pub port: u16,
    /// This launch's UI write credential, or None if it could not be minted
    /// (the sidecar then refuses every write with 503 rather than run open).
    pub write_token: Option<String>,
    pub write_token_file: Option<PathBuf>,
}

const WRITE_TOKEN_FILE_NAME: &str = "ui-write-token";

/// Mints this launch's UI write credential (ui/lib/write-auth.ts): 32 random
/// bytes as hex, written 0600 to a fresh file in the app's data dir. The
/// sidecar gets the file's path as PORTUNUS_WRITE_TOKEN_FILE; the window gets
/// the token in the URL fragment it navigates to (lib.rs), which never reaches
/// the server, a log, or another process's argv. A new token every launch.
fn mint_write_token(dir: &Path) -> std::io::Result<(PathBuf, String)> {
    let mut bytes = [0u8; 32];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    let token: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    fs::create_dir_all(dir)?;
    let path = dir.join(WRITE_TOKEN_FILE_NAME);
    match fs::remove_file(&path) {
        Err(e) if e.kind() != ErrorKind::NotFound => return Err(e),
        _ => {}
    }
    // create_new = O_CREAT|O_EXCL: never opens an existing file or a link.
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options.open(&path)?;
    file.write_all(token.as_bytes())?;
    Ok((path, token))
}

/// Spawns `node <server.js>` with PORT=<port> and the captured real PATH.
/// Does not wait for readiness -- call `wait_until_healthy` separately so
/// the caller can show a loading UI in the meantime.
pub fn spawn_sidecar(app: &AppHandle) -> SidecarHandle {
    let server_js = resolve_server_js(app);
    let port = pick_free_port();
    let path = capture_login_shell_path();
    let minted = app
        .path()
        .app_data_dir()
        .map_err(|e| std::io::Error::new(ErrorKind::Other, e.to_string()))
        .and_then(|dir| mint_write_token(&dir));
    let (write_token_file, write_token) = match minted {
        Ok((file, token)) => (Some(file), Some(token)),
        Err(e) => {
            log::error!("could not mint the UI write token, so the vault UI will refuse writes: {e}");
            (None, None)
        }
    };

    let mut command = Command::new("node");
    command
        .arg(&server_js)
        .env("PORT", port.to_string())
        .env("PATH", path)
        .env("HOSTNAME", "127.0.0.1")
        // Never inherit an opt-out or another token file from the launching env.
        .env_remove("PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES")
        .env_remove("PORTUNUS_WRITE_TOKEN_FILE");
    if let Some(file) = &write_token_file {
        command.env("PORTUNUS_WRITE_TOKEN_FILE", file);
    }
    let child = command
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap_or_else(|e| {
            panic!("failed to spawn sidecar (node {}): {e}", server_js.display())
        });

    SidecarHandle { child, port, write_token, write_token_file }
}

/// Polls http://127.0.0.1:<port>/api/health?shallow=1 until it returns 200,
/// or gives up after HEALTH_POLL_TIMEOUT. Returns true if the sidecar became
/// healthy. Shallow on purpose: readiness means "the Next.js server answers",
/// and the deep check spawns the CLI and can legitimately report down (e.g. a
/// corrupt registry) while the UI still needs to open to show that.
pub fn wait_until_healthy(port: u16) -> bool {
    let url = format!("http://127.0.0.1:{port}/api/health?shallow=1");
    let deadline = std::time::Instant::now() + HEALTH_POLL_TIMEOUT;
    while std::time::Instant::now() < deadline {
        if let Ok(resp) = ureq::get(&url).timeout(Duration::from_secs(2)).call() {
            if resp.status() == 200 {
                return true;
            }
        }
        std::thread::sleep(HEALTH_POLL_INTERVAL);
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = env::temp_dir().join(format!("portunus-sidecar-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn mints_a_fresh_0600_hex_token_each_time() {
        let dir = scratch("mint");
        let (path, first) = mint_write_token(&dir).unwrap();
        assert_eq!(first.len(), 64);
        assert!(first.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(fs::read_to_string(&path).unwrap(), first);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        let (_, second) = mint_write_token(&dir).unwrap();
        assert_ne!(first, second);
        assert_eq!(fs::read_to_string(&path).unwrap(), second);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn never_writes_through_a_planted_symlink() {
        let dir = scratch("link");
        fs::create_dir_all(&dir).unwrap();
        let victim = dir.join("victim");
        fs::write(&victim, "untouched").unwrap();
        std::os::unix::fs::symlink(&victim, dir.join(WRITE_TOKEN_FILE_NAME)).unwrap();
        let (path, token) = mint_write_token(&dir).unwrap();
        assert_eq!(fs::read_to_string(&victim).unwrap(), "untouched");
        assert!(!fs::symlink_metadata(&path).unwrap().file_type().is_symlink());
        assert_eq!(fs::read_to_string(&path).unwrap(), token);
        fs::remove_dir_all(&dir).unwrap();
    }
}
