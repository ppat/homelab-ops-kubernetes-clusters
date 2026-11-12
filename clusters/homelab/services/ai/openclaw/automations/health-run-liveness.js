// openclaw script payload for the health-run-liveness automation. It runs headless in the
// gateway's scheduler, with no model turn: whatever it returns as `notify` is sent as is, and
// returning nothing sends nothing. reconcile.js puts a quiet-hours check in front of it.
//
// It reads n8n's last-run endpoint. Only an explicit "ok": true stays silent; any other answer,
// or no answer, is reported. The request must use the Service name: the endpoint answers only
// when the Host header is that name, and it is the one private host openclaw.json's
// tools.web.fetch.ssrfPolicy lets web_fetch reach.
const URL = "http://n8n.n8n.svc.cluster.local:5678/webhook/health/last-run";
const UNREACHABLE = "Health check did not run: n8n unreachable.";

let result;
try {
  result = await web_fetch({ url: URL });
} catch (err) {
  return { notify: UNREACHABLE };
}

// web_fetch wraps the body as untrusted external content: a header, a line "---", the body,
// then an end marker.
function body(text) {
  const start = text.indexOf("---\n");
  const end = text.lastIndexOf("<<<END_EXTERNAL_UNTRUSTED_CONTENT");
  if (start < 0 || end < start) return null;
  try {
    return JSON.parse(text.slice(start + 4, end).trim());
  } catch {
    return null;
  }
}

const answer = result && result.status === 200 ? body(String(result.text ?? "")) : null;
if (!answer || typeof answer.ok !== "boolean") return { notify: UNREACHABLE };
if (answer.ok) return {};
const message = typeof answer.message === "string" ? answer.message.trim().slice(0, 1000) : "";
return { notify: message || "Health check did not run." };
