mod sidecar;

use std::process::Child;
use std::sync::Mutex;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// Holds the spawned sidecar so it isn't silently orphaned by a dropped
/// handle -- story 02 wires an explicit kill on Quit to this state.
pub struct SidecarState {
    pub child: Mutex<Option<Child>>,
    pub port: u16,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
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

            // Show the loading placeholder immediately; swap to the real
            // sidecar URL (or show an error state) once health-checked --
            // never a browser connection-refused page (story 01 AC #4).
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Portunus Vault")
                .inner_size(1200.0, 800.0)
                .visible(true)
                .build()?;

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
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
