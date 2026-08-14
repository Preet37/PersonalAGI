// The shell's whole job: call the CLI, render text, record outcomes.
//
// It holds no state the CLI does not, and it can do nothing the terminal
// cannot — every action is a fixed subcommand invoked by name. That is
// deliberate: this window renders text written by strangers, so it must not be
// able to assemble a command line.

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const out = document.getElementById("out");
const stateEl = document.getElementById("state");
const stateText = document.getElementById("state-text");

// A proposal line from the ledger:
//   "4a06bd54  pending   [suppressed]  rationale..."
const LEDGER_LINE = /^([0-9a-f]{6,})\s+(\w+)\s*(\[suppressed\])?\s*(.*)$/;

// Sweep output marks attention in the left gutter. Mapping it to a colour is
// the one piece of interpretation this file does, and getting it wrong makes
// everything look equally urgent — which is how people stop reading.
function attentionOf(line) {
  if (line.startsWith("!!!")) return "interrupt";
  if (line.startsWith(" ! ")) return "nudge";
  return "ambient";
}

function escape(text) {
  const node = document.createElement("span");
  node.textContent = text;
  return node.innerHTML;
}

function renderText(text, isError) {
  out.innerHTML = "";
  const pre = document.createElement("pre");
  if (isError) pre.className = "err";
  pre.textContent = text.trim() || "(nothing)";
  out.appendChild(pre);
}

/** The proposal card: rationale, why, and the five outcomes. */
function renderProposals(text) {
  const cards = [];
  for (const raw of text.split("\n")) {
    const match = LEDGER_LINE.exec(raw.trim());
    if (!match) continue;
    const [, id, outcome, suppressed, rationale] = match;
    if (outcome !== "pending") continue;
    cards.push({ id, rationale, suppressed: Boolean(suppressed) });
  }

  if (!cards.length) {
    // Saying nothing is the most common CORRECT output of a proactive system,
    // so it gets a real message rather than an empty panel.
    renderText(
      "Nothing waiting.\n\nFor a proactive system that is the most common " +
        "correct answer — but check “What it didn't say” to see what " +
        "fell below the bar."
    );
    return;
  }

  out.innerHTML = "";
  for (const card of cards) {
    const el = document.createElement("div");
    el.className = "card " + attentionOf(card.rationale);
    el.innerHTML = `
      <div>${escape(card.rationale)}</div>
      <div class="why">${card.suppressed ? "suppressed · " : ""}${card.id}</div>
      <div class="acts">
        ${["accepted", "edited", "dismissed", "ignored", "reversed"]
          .map((o) => `<button data-id="${card.id}" data-outcome="${o}">${o}</button>`)
          .join("")}
      </div>`;
    out.appendChild(el);
  }
}

async function call(command, args = {}) {
  out.innerHTML = '<p class="hint">Working…</p>';
  try {
    const result = await invoke(command, args);
    return result;
  } catch (err) {
    return { ok: false, output: String(err) };
  }
}

async function show(command) {
  const result = await call(command);
  if (command === "proposals" || command === "suppressed") {
    if (result.ok) return renderProposals(result.output);
  }
  renderText(result.output, !result.ok);
}

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("on"));
    button.classList.add("on");
    const cmd = button.dataset.cmd;
    // "What needs me" runs the sweep, then shows what is waiting.
    if (cmd === "sweep") {
      await call("sweep");
      return show("proposals");
    }
    return show(cmd);
  });
});

// Outcome buttons are delegated: cards are re-rendered constantly.
out.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-outcome]");
  if (!button) return;
  const result = await call("feedback", {
    id: button.dataset.id,
    outcome: button.dataset.outcome,
  });
  if (!result.ok) return renderText(result.output, true);
  return show("proposals");
});

// Capture state. Driven by the Rust side so the indicator can never disagree
// with reality — the whole open-means-active design depends on that.
function setCapture(active) {
  stateEl.classList.toggle("live", active);
  stateText.textContent = active ? "active" : "halted";
}

listen("capture-changed", (event) => setCapture(event.payload));
invoke("capture_state").then(setCapture).catch(() => setCapture(false));

show("proposals");
