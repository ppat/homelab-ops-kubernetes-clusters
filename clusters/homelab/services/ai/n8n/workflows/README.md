# n8n Seed Workflows

Example n8n workflow exports for this cluster's day-one integrations. n8n↔OpenClaw wiring is
cluster-specific behavior, so it lives here rather than in the `apps-ai` module's n8n slice
(which is OpenClaw-agnostic). These are **not** auto-imported by GitOps — workflows live in
n8n's Postgres database, not the filesystem, so importing them is a one-time, manual,
not-GitOps-able bootstrap step (same category as OpenClaw's WhatsApp QR pairing). Re-running the
import is safe (it upserts by workflow ID) but will overwrite any in-UI edits made to these
specific workflows.

| File | Purpose |
| ---- | ------- |
| `alertmanager-receiver.json` | Alertmanager webhook → dedup on `status: firing` → one-line LiteLLM (`openai/gpt-5-nano`) summary → relay to OpenClaw's `/hooks/agent` → WhatsApp |
| `notify-subworkflow.json` | Shared "Notify" sub-workflow (`Execute Workflow` target) — fans out a `subject`/`message` pair to Maddy SMTP and OpenClaw |
| `media-downloaded-notify.json` | Example Radarr/Sonarr "downloaded" webhook (header-auth) → builds a notify payload → calls the shared Notify sub-workflow |

## Importing

The import step itself (copying these into the n8n pod and running `n8n import:workflow`) is
tracked as part of the cluster rollout, not performed here. `notify-subworkflow.json` must be
imported before `media-downloaded-notify.json` — the latter references it by workflow name via
an `Execute Workflow` node, which n8n resolves post-import in the UI.

Both pre-seeded credentials referenced by these workflows (LiteLLM Bearer, Maddy SMTP) come from
`apps-ai`'s `n8n_credentials_overwrite` secret-store key — no manual credential setup is required,
only the workflow import above.

**Before importing**, edit `notify-subworkflow.json`'s email node: the `${domain_name}` tokens in
its `fromEmail`/`toEmail` fields are Flux postBuild placeholders that only get substituted in
GitOps-applied manifests, not on a manual `n8n import:workflow`. Replace `fromEmail` with the real
SMTP sender (BWS key `cluster_homelab_n8n_smtp_sender`) and `toEmail` with the real notification
recipient before importing.
