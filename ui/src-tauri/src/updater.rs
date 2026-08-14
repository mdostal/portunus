//! gh-CLI-backed update check -- not Tauri's built-in updater plugin. The
//! repo is private; a public signed-feed updater would need an embedded
//! GitHub token in the shipped app, which is exactly the credential-in-a-
//! binary anti-pattern this whole project exists to prevent. Instead this
//! shells out to `gh`, which already holds the user's own credential the
//! same way `portunus` itself never touches a credential it doesn't have
//! to (design-discussion.md §4).
//!
//! "Auto-update" here means auto-*checked*, one-click-confirmed -- never a
//! silent unattended swap. Replacing a running vault app out from under
//! the user is consequential enough to ask.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

const REPO: &str = "mdostal/portunus";
const ASSET_PATTERN: &str = "portunus-desktop-*.zip";
const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

/// Pure, testable: is `latest_tag` (e.g. "v0.16.0") newer than the running
/// app's own `current` version (e.g. "0.15.0", no `v` prefix -- matches
/// CARGO_PKG_VERSION's format)? Errors on either failing to parse as
/// semver -- never silently treats malformed input as "not newer".
pub fn is_newer(current: &str, latest_tag: &str) -> Result<bool, String> {
    let latest = latest_tag.trim_start_matches('v');
    let current_v = semver::Version::parse(current)
        .map_err(|e| format!("bad current version {current:?}: {e}"))?;
    let latest_v = semver::Version::parse(latest)
        .map_err(|e| format!("bad latest tag {latest_tag:?}: {e}"))?;
    Ok(latest_v > current_v)
}

/// `gh release view --repo <repo> latest --json tagName --jq .tagName` --
/// the user's own already-authenticated gh CLI, never a token this app
/// holds itself.
fn check_latest_release_tag() -> Result<String, String> {
    let output = Command::new("gh")
        .args([
            "release", "view", "--repo", REPO, "latest",
            "--json", "tagName", "--jq", ".tagName",
        ])
        .output()
        .map_err(|e| format!("failed to run gh: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "gh release view failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let tag = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if tag.is_empty() {
        return Err("gh release view returned an empty tag".to_string());
    }
    Ok(tag)
}

fn download_release_asset(tag: &str, dest_dir: &Path) -> Result<PathBuf, String> {
    let status = Command::new("gh")
        .args([
            "release", "download", tag, "--repo", REPO,
            "--pattern", ASSET_PATTERN, "--dir", dest_dir.to_str().unwrap_or("."),
            "--clobber",
        ])
        .status()
        .map_err(|e| format!("failed to run gh release download: {e}"))?;
    if !status.success() {
        return Err(format!("gh release download failed for tag {tag}"));
    }
    std::fs::read_dir(dest_dir)
        .map_err(|e| format!("failed to read download dir: {e}"))?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .find(|p| p.extension().is_some_and(|ext| ext == "zip"))
        .ok_or_else(|| "gh release download reported success but no .zip was found".to_string())
}

/// Spawns the bundled relauncher.sh as a DETACHED process, then exits this
/// app -- a process cannot safely overwrite its own running binary, so a
/// separate helper does the actual swap after this one is gone.
fn spawn_relauncher_and_exit(app: &AppHandle, zip_path: &Path) {
    let installed_app = current_app_bundle_path(app);
    let relauncher = match app.path().resource_dir() {
        Ok(dir) => dir.join("relauncher.sh"),
        Err(e) => {
            log::error!("could not resolve resource dir for relauncher: {e}");
            return;
        }
    };
    let my_pid = std::process::id().to_string();

    match Command::new("/bin/bash")
        .arg(&relauncher)
        .arg(zip_path)
        .arg(&installed_app)
        .arg(&my_pid)
        .spawn()
    {
        Ok(_) => {
            log::info!("relauncher spawned, exiting for swap");
            app.exit(0);
        }
        Err(e) => {
            log::error!("failed to spawn relauncher: {e}");
        }
    }
}

fn current_app_bundle_path(app: &AppHandle) -> PathBuf {
    // The running binary lives at <App>.app/Contents/MacOS/<bin>; the
    // installed bundle root is three levels up.
    app.path()
        .resource_dir()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf())) // Contents/
        .and_then(|p| p.parent().map(|p| p.to_path_buf())) // <App>.app
        .unwrap_or_else(|| PathBuf::from("/Applications/Portunus.app"))
}

fn prompt_and_maybe_update(app: &AppHandle, tag: String) {
    let app_for_dialog = app.clone();
    app.dialog()
        .message(format!(
            "Portunus {tag} is available (you're running {}). Install and relaunch now?",
            app.package_info().version
        ))
        .title("Portunus Update Available")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Update Now".into(),
            "Later".into(),
        ))
        .show(move |confirmed| {
            if !confirmed {
                return;
            }
            let app = app_for_dialog.clone();
            std::thread::spawn(move || {
                let tmp = match tempdir() {
                    Ok(d) => d,
                    Err(e) => {
                        log::error!("could not create temp dir for update download: {e}");
                        return;
                    }
                };
                match download_release_asset(&tag, &tmp) {
                    Ok(zip_path) => spawn_relauncher_and_exit(&app, &zip_path),
                    Err(e) => log::error!("update download failed: {e}"),
                }
            });
        });
}

fn tempdir() -> std::io::Result<PathBuf> {
    let dir = std::env::temp_dir().join(format!("portunus-update-{}", std::process::id()));
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

/// One check: compares the latest release tag against the running app's
/// own version. `notify_if_none` controls whether a "no update"
/// confirmation is shown -- true for the manual tray item (a click should
/// always get feedback), false for the silent background timer (a no-op
/// on every 6h tick would be noisy).
fn run_check(app: &AppHandle, notify_if_none: bool) {
    let current = app.package_info().version.to_string();
    match check_latest_release_tag() {
        Ok(tag) => match is_newer(&current, &tag) {
            Ok(true) => prompt_and_maybe_update(app, tag),
            Ok(false) => {
                if notify_if_none {
                    app.dialog()
                        .message(format!("You're up to date (v{current})."))
                        .title("Portunus")
                        .kind(MessageDialogKind::Info)
                        .buttons(MessageDialogButtons::Ok)
                        .show(|_| {});
                }
            }
            Err(e) => log::warn!("version comparison failed: {e}"),
        },
        Err(e) => {
            log::warn!("update check failed: {e}");
            if notify_if_none {
                app.dialog()
                    .message(format!("Couldn't check for updates: {e}"))
                    .title("Portunus")
                    .kind(MessageDialogKind::Error)
                    .buttons(MessageDialogButtons::Ok)
                    .show(|_| {});
            }
        }
    }
}

/// Manual "Check for Updates" tray item -- always gives feedback.
pub fn check_now(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || run_check(&app, true));
}

/// Background timer -- silent unless an update is actually found.
pub fn spawn_background_checker(app: AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(CHECK_INTERVAL);
        run_check(&app, false);
    });
}

#[cfg(test)]
mod tests {
    use super::is_newer;

    #[test]
    fn newer_tag_is_newer() {
        assert_eq!(is_newer("0.15.0", "v0.16.0"), Ok(true));
    }

    #[test]
    fn equal_tag_is_not_newer() {
        assert_eq!(is_newer("0.15.0", "v0.15.0"), Ok(false));
    }

    #[test]
    fn older_tag_is_not_newer() {
        assert_eq!(is_newer("0.15.0", "v0.14.0"), Ok(false));
    }

    #[test]
    fn malformed_current_is_an_error() {
        assert!(is_newer("not-a-version", "v0.15.0").is_err());
    }

    #[test]
    fn malformed_tag_is_an_error() {
        assert!(is_newer("0.15.0", "not-a-version").is_err());
    }

    #[test]
    fn tag_without_v_prefix_still_parses() {
        assert_eq!(is_newer("0.15.0", "0.16.0"), Ok(true));
    }
}
