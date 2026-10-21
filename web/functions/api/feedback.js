// /api/feedback: thumbs from digest pages, stored in D1 (schema in web/schema.sql).
//   GET  -> {paper_id: latest rating} so the page can show existing votes
//   POST {paper_id, rating: 1|-1, note?} -> appends a row
// The whole site sits behind Cloudflare Access, which adds the signed-in email header.
// Without it we refuse, so a misconfigured Access app fails closed. Set the Pages
// variable REQUIRE_ACCESS=false only for local `wrangler pages dev`.

function signedIn(request, env) {
  const email = request.headers.get("cf-access-authenticated-user-email");
  if (email) return email;
  return env.REQUIRE_ACCESS === "false" ? "local" : null;
}

const denied = () => new Response("Sign in through Cloudflare Access first.", { status: 403 });
const bad = (msg) => new Response(msg, { status: 400 });

export async function onRequestGet({ request, env }) {
  if (!signedIn(request, env)) return denied();
  const { results } = await env.DB.prepare(
    "SELECT paper_id, rating FROM feedback WHERE id IN (SELECT MAX(id) FROM feedback GROUP BY paper_id)",
  ).all();
  return Response.json(Object.fromEntries(results.map((r) => [r.paper_id, r.rating])));
}

export async function onRequestPost({ request, env }) {
  const email = signedIn(request, env);
  if (!email) return denied();
  // JSON-only, so a cross-site form post can't ride the Access cookie.
  if (!(request.headers.get("content-type") || "").startsWith("application/json")) {
    return new Response("Expected application/json", { status: 415 });
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return bad("Invalid JSON");
  }
  const paperId = typeof body.paper_id === "string" ? body.paper_id.trim() : "";
  const rating = body.rating === 1 || body.rating === -1 ? body.rating : null;
  const note = typeof body.note === "string" && body.note.trim() ? body.note.trim().slice(0, 1000) : null;
  if (!paperId || paperId.length > 300 || rating === null) return bad("Need paper_id and rating of 1 or -1");
  const at = new Date().toISOString().replace(/\.\d+Z$/, "+00:00");
  await env.DB.prepare("INSERT INTO feedback (paper_id, rating, note, at, user_email) VALUES (?, ?, ?, ?, ?)")
    .bind(paperId, rating, note, at, email)
    .run();
  return Response.json({ ok: true });
}
