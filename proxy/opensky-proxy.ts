// Minimal OpenSky proxy for GitHub Actions, because OpenSky firewalls
// datacenter IPs (verified 2026-07-18: runners get connect timeouts while
// residential IPs answer in 0.5s). Deploy on Deno Deploy — separate from the
// THY globe worker, do not merge them.
//
// Env vars (set in the Deno Deploy project):
//   OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET  — your OpenSky API client
//   PROXY_KEY                                 — shared secret; the collector
//                                               sends it as the x-proxy-key header
// Precise scheduler: GitHub's own cron queue fires 30-90+ min late, so this
// worker dispatches the workflow instead — Deno.cron is on-the-minute and
// dispatched workflows start within seconds. Extra env var: GITHUB_TOKEN
// (fine-grained PAT, Actions read+write on ist-delay-dataset only).
// Once this is live, delete the schedule: block from collect.yml.
Deno.cron("trigger IST collection", "45 18 * * *", async () => {
  const r = await fetch(
    "https://api.github.com/repos/ahmethamzamulayim-png/ist-delay-dataset/actions/workflows/collect.yml/dispatches",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${Deno.env.get("GITHUB_TOKEN")}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  console.log("workflow dispatch:", r.status, r.ok ? "ok" : await r.text());
});

const TOKEN_URL =
  "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token";
const API = "https://opensky-network.org/api";
const ALLOWED = new Set(["/flights/departure", "/flights/arrival"]);

let token = "";
let tokenExp = 0;

async function getToken(): Promise<string> {
  if (token && Date.now() < tokenExp - 60_000) return token;
  const r = await fetch(TOKEN_URL, {
    method: "POST",
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: Deno.env.get("OPENSKY_CLIENT_ID") ?? "",
      client_secret: Deno.env.get("OPENSKY_CLIENT_SECRET") ?? "",
    }),
  });
  if (!r.ok) throw new Error(`token fetch failed: HTTP ${r.status}`);
  const j = await r.json();
  token = j.access_token;
  tokenExp = Date.now() + (j.expires_in ?? 1800) * 1000;
  return token;
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  if (req.headers.get("x-proxy-key") !== Deno.env.get("PROXY_KEY")) {
    return new Response("forbidden", { status: 403 });
  }
  if (!ALLOWED.has(url.pathname)) {
    return new Response("not found", { status: 404 });
  }
  try {
    const t = await getToken();
    const r = await fetch(API + url.pathname + url.search, {
      headers: { Authorization: `Bearer ${t}` },
    });
    return new Response(await r.text(), {
      status: r.status,
      headers: {
        "content-type": r.headers.get("content-type") ?? "application/json",
      },
    });
  } catch (e) {
    return new Response(`proxy error: ${e}`, { status: 502 });
  }
});
