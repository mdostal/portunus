mod sidecar;
mod tray;

use std::process::Child;
use std::sync::Mutex;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

/// Holds the spawned sidecar so it isn't silently orphaned by a dropped
/// handle -- Quit (any path: tray menu, Cmd+Q, Dock) kills it explicitly
/// via the RunEvent::ExitRequested handler below, the one place this is
/// guaranteed to run regardless of how quit was triggered.
pub struct SidecarState {
    pub child: Mutex<Option<Child>>,
    pub port: u16,
}

fn kill_sidecar(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<SidecarState>() {
        if let Ok(mut guard) = state.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        // Must be registered first (documented Tauri requirement) -- a
        // second launch focuses the existing window instead of spawning a
        // second sidecar and colliding on setup.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            tray::show_main_window(app);
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let handle = sidecar::spawn_sidecar(app.handle());
            let port = handle.port;
            app.manage(SidecarState {
                child: Mutex::new(Some(handle.child)),
                port,
            });

            tray::build_tray(app.handle())?;

            // Show the loading placeholder immediately; swap to the real
            // sidecar URL (or show an error state) once health-checked --
            // never a browser connection-refused page (story 01 AC #4).
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Portunus Vault")
                .inner_size(1200.0, 800.0)
                .visible(true)
                .build()?;

            // Close-to-tray: the window hides, the sidecar keeps running,
            // the tray icon stays -- standard menu-bar-app UX. Only the
            // explicit Quit path (tray menu / Cmd+Q / Dock) actually
            // terminates, handled once in RunEvent::ExitRequested below.
            let window_for_close = window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window_for_close.hide();
                }
            });

            let window_for_thread = window.clone();
            std::thread::spawn(move || {
                if sidecar::wait_until_healthy(port) {
                    let url = format!("http://127.0.0.1:{port}");
                    if let Ok(parsed) = tauri::Url::parse(&url) {
                        let _ = window_for_thread.navigate(parsed);
                    }
                } else {
                    let _ = window_for_thread.eval(
                        "document.getElementById('spinner').style.display='none';\
                         document.getElementById('status').style.display='none';\
                         var e=document.getElementById('err');\
                         e.style.display='block';\
                         e.textContent='Portunus Vault did not start in time. \
                         Check that node and the portunus CLI are installed and on PATH, \
                         then relaunch.';",
                    );
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    builder.run(|app_handle, event| {
        // The one place sidecar cleanup is guaranteed to run, regardless
        // of which quit path triggered it (tray "Quit", Cmd+Q, Dock menu,
        // or app.exit() from tray.rs).
        if let RunEvent::ExitRequested { .. } = event {
            kill_sidecar(app_handle);
        }
    });
}
