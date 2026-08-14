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

use std::path::PathBuf;
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

/// Find the interpreter that has personalagi installed.
///
/// A Finder-launched .app inherits almost no PATH -- not the shell's, and
/// certainly not an activated virtualenv. Calling bare `python` works when the
/// app is started from a terminal and fails the moment it is double-clicked,
/// which is the way it will actually be used.
///
/// PERSONALAGI_PYTHON wins if set. Otherwise the project's own .venv, which is
/// where the package actually lives. `python3` on PATH is the last resort, and
/// it is genuinely a resort: it will usually be the system Python with none of
/// the dependencies.
fn interpreter() -> PathBuf {
    if let Ok(explicit) = std::env::var("PERSONALAGI_PYTHON") {
        let path = PathBuf::from(explicit);
        if path.exists() {
            return path;
        }
    }

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(root) = std::env::var("PERSONALAGI_ROOT") {
        candidates.push(PathBuf::from(root).join(".venv/bin/python"));
    }
    // The repo checkout, relative to the compiled binary at
    // desktop/src-tauri/target/{debug,release}/.
    if let Ok(exe) = std::env::current_exe() {
        let mut walk = exe.as_path();
        for _ in 0..6 {
            if let Some(parent) = walk.parent() {
                candidates.push(parent.join(".venv/bin/python"));
                walk = parent;
            }
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(PathBuf::from(home).join("PersonalAGI/.venv/bin/python"));
    }

    candidates
        .into_iter()
        .find(|p| p.exists())
        .unwrap_or_else(|| PathBuf::from("python3"))
}

/// The directory to run the CLI from, so it reads the right .env and database.
fn working_dir() -> Option<PathBuf> {
    interpreter()
        .parent()          // .venv/bin
        .and_then(|p| p.parent())  // .venv
        .and_then(|p| p.parent())  // project root
        .map(PathBuf::from)
        .filter(|p| p.join("src/personalagi").exists())
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

    let python = interpreter();
    let mut command = Command::new(&python);
    command.arg("-m").arg("personalagi").args(args);
    if let Some(dir) = working_dir() {
        // Without this the CLI runs from wherever Finder launched the app and
        // silently uses a different (empty) database.
        command.current_dir(dir);
    }

    match command.output() {
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
                 Tried: {}\n\
                 Set PERSONALAGI_PYTHON to your venv's python if this is wrong.",
                python.display()
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
