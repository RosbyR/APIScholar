// Thumb buttons on digest pages. Votes go to /api/feedback (Pages Function -> D1);
// the next scheduled run pulls them into paperbot's store as feedback + calibration labels.
// The page says where votes go: Roz's /api/feedback, or a friend's /api/me/votes.
const API = document.body.dataset.api || "/api/feedback";
const ASK_NOTE = document.body.dataset.notes !== "off";

function mark(id, rating) {
  for (const box of document.querySelectorAll(`.fb[data-id="${CSS.escape(id)}"]`)) {
    for (const b of box.querySelectorAll("button")) {
      b.setAttribute("aria-pressed", String(Number(b.dataset.r) === rating));
    }
  }
}

async function load() {
  if (!document.querySelector(".fb")) return;
  try {
    const r = await fetch(API, { credentials: "same-origin" });
    if (!r.ok) return;
    for (const [id, rating] of Object.entries(await r.json())) mark(id, rating);
  } catch {
    // Votes just won't show as pressed; clicking still works.
  }
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest(".fb button");
  if (!b || b.getAttribute("aria-pressed") === "true") return;
  const box = b.closest(".fb");
  const msg = box.querySelector(".fb-msg");
  const rating = Number(b.dataset.r);
  let note = null;
  if (rating < 0 && ASK_NOTE) {
    note = prompt("Optional: what made this one miss? (leave blank to skip)", "");
    if (note === null) return; // cancelled
  }
  msg.textContent = "saving…";
  try {
    const r = await fetch(API, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paper_id: box.dataset.id, rating, note: note || null }),
    });
    if (!r.ok) throw new Error(await r.text());
    mark(box.dataset.id, rating);
    msg.textContent = "saved";
  } catch (err) {
    msg.textContent = "not saved, try again";
    console.error(err);
  }
});

// Buttons that start paid work (Jev, Claude) ask first.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// While a background job runs, the status box asks for a refresh every few seconds.
if (document.querySelector("[data-poll]")) setTimeout(() => location.reload(), 4000);

load();
