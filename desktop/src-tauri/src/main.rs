// PersonalAGI desktop shell.
//
// A thin window over the CLI. Every command shells out to `python -m
// personalagi`, so the shell can never do anything the terminal cannot and
// there is exactly one set of rules to reason about.
//
// THE DESIGN DECISION WORTH DEFENDING
// Open means active. Closed means it is not watching. The window's visibility
// IS the capture state, not a setting inside a window that keeps running after
// you close it. That makes the state visible at a glance, makes turning it off
// a physical gesture, and doubles as the consent mechanism when transcription
// is added. A tray icon that keeps listening after the window closes is the
// design that gets an assistant uninstalled.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tauri::{Emitter, Manager, State, WindowEvent};

/// Whether the system is currently permitted to observe anything.
///
/// Not a preference. Closing the window sets it false, and every command
/// checks it — so "I closed it" and "it stopped" cannot drift apart.
struct Capture(Arc<AtomicBool>);

#[derive(serde::Serialize)]
struct CliResult {
    ok: bool,
    output: String,
}

/// Run a CLI subcommand and hand back its text.
///
/// The argument list is fixed per command below and never assembled from
/// anything the page sends. A renderer that could pass arbitrary argv would be
/// a shell injection surface reachable from any page bug, and this window
/// displays text written by strangers.
fn run(capture: &Capture, args: &[&str]) -> CliResult {
    if !capture.0.load(Ordering::SeqCst) {
        return CliResult {
            ok: false,
            output: "Capture is halted. Open the window to resume.".into(),
        };
    }

    match Command::new("python").arg("-m").arg("personalagi").args(args).output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            CliResult {
                ok: out.status.success(),
                // stderr carries the CLI's own error text, which is written to
                // be read by a person. Dropping it would turn a clear failure
                // into a blank panel.
                output: if stdout.trim().is_empty() { stderr } else { stdout },
            }
        }
        Err(err) => CliResult {
            ok: false,
            output: format!(
                "could not run the CLI: {err}\n\
                 The desktop shell requires `python -m personalagi` on PATH."
            ),
        },
    }
}

#[tauri::command]
fn sweep(capture: State<Capture>) -> CliResult {
    run(&capture, &["sweep"])
}

#[tauri::command]
fn proposals(capture: State<Capture>) -> CliResult {
    run(&capture, &["proposals", "--limit", "20"])
}

#[tauri::command]
fn suppressed(capture: State<Capture>) -> CliResult {
    // Kept one click away, not buried. Once the system starts staying quiet
    // its blind spots become invisible to the person relying on it.
    run(&capture, &["proposals", "--explore"])
}

#[tauri::command]
fn owed(capture: State<Capture>) -> CliResult {
    run(&capture, &["owed"])
}

#[tauri::command]
fn brief(capture: State<Capture>) -> CliResult {
    run(&capture, &["brief", "--days", "2"])
}

#[tauri::command]
fn gaps(capture: State<Capture>) -> CliResult {
    run(&capture, &["goal", "gaps"])
}

/// Record an outcome. The feedback loop's only entry point from the UI.
#[tauri::command]
fn feedback(capture: State<Capture>, id: String, outcome: String) -> CliResult {
    // Whitelisted, not interpolated. The renderer cannot invent an outcome.
    let allowed = ["accepted", "edited", "dismissed", "ignored", "reversed"];
    if !allowed.contains(&outcome.as_str()) {
        return CliResult { ok: false, output: format!("unknown outcome: {outcome}") };
    }
    // Ids are hex from the ledger; anything else is refused rather than passed
    // to a subprocess.
    if id.is_empty() || !id.chars().all(|c| c.is_ascii_alphanumeric()) {
        return CliResult { ok: false, output: "invalid proposal id".into() };
    }
    run(&capture, &["feedback", &id, &outcome])
}

#[tauri::command]
fn capture_state(capture: State<Capture>) -> bool {
    capture.0.load(Ordering::SeqCst)
}

/// Stop observing. Called before the window goes, never after.
#[tauri::command]
fn halt_capture(capture: State<Capture>) -> bool {
    capture.0.store(false, Ordering::SeqCst);
    false
}

fn main() {
    let capture = Arc::new(AtomicBool::new(true));

    tauri::Builder::default()
        .manage(Capture(capture.clone()))
        .invoke_handler(tauri::generate_handler![
            sweep, proposals, suppressed, owed, brief, gaps, feedback,
            capture_state, halt_capture
        ])
        .on_window_event({
            let capture = capture.clone();
            move |window, event| {
                match event {
                    // Halt BEFORE the window disappears. If this ran after,
                    // there would be a window in which the UI is gone and the
                    // system is still watching — which is precisely the state
                    // the whole open-means-active design exists to make
                    // impossible.
                    WindowEvent::CloseRequested { .. } => {
                        capture.store(false, Ordering::SeqCst);
                        let _ = window.emit("capture-changed", false);
                    }
                    WindowEvent::Focused(true) => {
                        capture.store(true, Ordering::SeqCst);
                        let _ = window.emit("capture-changed", true);
                    }
                    _ => {}
                }
            }
        })
        .setup(|app| {
            // Reflect the real state, not a remembered preference.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.emit("capture-changed", true);
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start PersonalAGI");
}
