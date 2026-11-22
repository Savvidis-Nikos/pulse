// Calls to the Pulse server. Nothing here computes analytics; refresh asks the server to run the next check.

async function get(path, opts = {}) {
  const r = await fetch(path, { cache: "no-store", ...opts });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).error || msg; } catch (e) { /* not JSON */ }
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export const api = {
  overview: () => get("/api/overview"),
  snapshot: id => get(`/api/snapshot/${encodeURIComponent(id)}`),
  interpretation: id => get(`/api/interpretation/${encodeURIComponent(id)}`),
  refresh: () => get("/api/refresh", { method: "POST" }),
};
