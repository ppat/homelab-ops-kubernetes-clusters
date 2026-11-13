// Makes openclaw's automations match the jobs defined beside this file. Arguments come in pairs:
// a job definition (JSON) and the job's script (JavaScript). It runs inside the gateway container
// (kubectl exec from the openclaw-automations-reconcile CronJob), because the CLI admits an
// operator only over loopback without a device pairing. Jobs not named in the arguments are left
// alone.
//
// Every job is a script payload: the scheduler runs the script headless and sends what it returns
// as `notify`, with no model turn, so neither the decision nor the message depends on LiteLLM.
//
// For each job: a stored job that matches the definition is left as it is. A missing, changed,
// disabled or duplicated one is removed and added again. The result is read back, and the run
// fails if it still differs, so the CronJob's failure rules see drift that cannot be corrected.
//
// Quiet hours. A job may deliver only while the operator is awake: the same window as
// agents.defaults.heartbeat.activeHours in openclaw.json. Two guards, both derived from
// ACTIVE_HOURS below.
//   - Every job's script starts with a check that returns, delivering nothing, unless the current
//     New York time leaves the whole run timeout inside the window. It runs whenever the job
//     runs: on schedule, in the catch-up the gateway runs at startup for a slot it missed while
//     down (which would otherwise deliver at whatever hour the gateway came back), and on a
//     manual `openclaw cron run`.
//   - A schedule with any occurrence outside that window is refused, so a wrong schedule fails
//     this CronJob instead of producing runs that the check silences.
const { spawnSync } = require("node:child_process");

const ACTIVE_HOURS = { start: 6 * 60, end: 22 * 60, tz: "America/New_York" };

function lastStart(timeoutSeconds) {
  return ACTIVE_HOURS.end - Math.ceil(timeoutSeconds / 60);
}

function quietHoursCheck(timeoutSeconds) {
  return [
    "// Added by openclaw-automations-reconcile: outside",
    `// ${hhmm(ACTIVE_HOURS.start)}-${hhmm(lastStart(timeoutSeconds))} ${ACTIVE_HOURS.tz}, return at once and deliver nothing.`,
    "if (!(() => {",
    `  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "${ACTIVE_HOURS.tz}", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date());`,
    "  const at = (type) => Number(parts.find((p) => p.type === type).value);",
    "  const minute = at(\"hour\") * 60 + at(\"minute\");",
    `  return minute >= ${ACTIVE_HOURS.start} && minute <= ${lastStart(timeoutSeconds)};`,
    "})()) return {};",
    "",
  ].join("\n");
}

// The CLI stores a script with surrounding whitespace trimmed, and a ConfigMap file ends in a
// newline, so the comparison is on the trimmed text.
function storedScript(job, script) {
  return (quietHoursCheck(job.timeoutSeconds) + script).trim();
}

function hhmm(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

// Accepts only what can be checked: a fixed minute and an explicit hour list or range.
function scheduleProblems(job) {
  if (job.tz !== ACTIVE_HOURS.tz) return [`tz must be ${ACTIVE_HOURS.tz}, got ${job.tz}`];
  const fields = String(job.cron).trim().split(/\s+/);
  if (fields.length !== 5) return [`cron must have 5 fields: ${job.cron}`];
  const [minute, hour] = fields;
  if (!/^\d+$/.test(minute) || Number(minute) > 59) return [`cron minute must be one number: ${minute}`];
  const hours = [];
  for (const part of hour.split(",")) {
    const m = part.match(/^(\d+)(?:-(\d+))?$/);
    if (!m) return [`cron hour must list hours or ranges: ${hour}`];
    for (let h = Number(m[1]); h <= Number(m[2] ?? m[1]); h++) hours.push(h);
  }
  const last = lastStart(job.timeoutSeconds);
  return hours
    .map((h) => h * 60 + Number(minute))
    .filter((start) => start < ACTIVE_HOURS.start || start > last)
    .map((start) => `runs at ${hhmm(start)}, outside ${hhmm(ACTIVE_HOURS.start)}-${hhmm(last)} ${ACTIVE_HOURS.tz}`);
}

// The fields of a stored job that define it. Everything else the gateway returns is run status
// (state, last-run and delivery fields appear and change after each run), and comparing it
// would replace the job after every run, resetting its failure count and with it its failure
// alerts.
const DEFINITION = ["name", "displayName", "description", "declarationKey", "agentId", "enabled",
  "schedule", "sessionTarget", "sessionKey", "wakeMode", "payload", "delivery", "failureAlert",
  "trigger", "pacing", "deleteAfterRun"];

function comparable(stored) {
  return Object.fromEntries(DEFINITION.map((key) => [key, stored[key]]));
}

function expected(job, script) {
  return {
    agentId: job.agentId,
    name: job.name,
    enabled: true,
    schedule: { kind: "cron", expr: job.cron, tz: job.tz, staggerMs: 0 },
    sessionTarget: "isolated",
    wakeMode: "now",
    payload: {
      kind: "script",
      timeoutSeconds: job.timeoutSeconds,
      toolBudget: job.toolBudget,
      toolsAllow: job.tools,
      script: storedScript(job, script),
    },
    delivery: { mode: "announce", channel: job.channel },
  };
}

function addArgs(job) {
  return ["cron", "add", "--json", "--name", job.name, "--agent", job.agentId,
    "--cron", job.cron, "--tz", job.tz, "--exact", "--session", "isolated",
    "--script", "-", "--script-timeout-seconds", String(job.timeoutSeconds),
    "--script-tool-budget", String(job.toolBudget), "--tools", job.tools.join(","),
    "--announce", "--channel", job.channel];
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().filter((k) => value[k] !== undefined).map((k) => [k, canonical(value[k])]));
  }
  return value;
}

function differences(want, have) {
  const keys = new Set([...Object.keys(want), ...Object.keys(have)]);
  return [...keys].filter((k) => JSON.stringify(canonical(want[k])) !== JSON.stringify(canonical(have[k])));
}

function cli(args, input) {
  const r = spawnSync("openclaw", args, { input, encoding: "utf8", timeout: 120000 });
  if (r.status !== 0) throw new Error(`openclaw ${args.slice(0, 2).join(" ")} failed (${r.status}): ${(r.stdout || "") + (r.stderr || "")}`.slice(0, 2000));
  return r.stdout ? JSON.parse(r.stdout) : null;
}

function named(name) {
  return cli(["cron", "list", "--all", "--json"]).jobs.filter((j) => j.name === name);
}

function reconcile(job, script) {
  const problems = scheduleProblems(job);
  if (problems.length) throw new Error(`${job.name}: schedule refused: ${problems.join("; ")}`);
  const want = canonical(expected(job, script));
  const found = named(job.name);
  const reason = found.length === 0 ? "missing"
    : found.length > 1 ? `${found.length} jobs with this name`
      : differences(want, comparable(found[0])).map((k) => `${k} differs`).join(", ");
  if (!reason) return console.log(`${job.name}: unchanged (${found[0].id})`);
  for (const stale of found) cli(["cron", "rm", "--json", stale.id]);
  cli(addArgs(job), storedScript(job, script));
  const after = named(job.name);
  const left = after.length === 1 ? differences(want, comparable(after[0])) : ["count"];
  if (left.length) throw new Error(`${job.name}: still differs after replace: ${left.join(", ")}`);
  console.log(`${job.name}: replaced (${reason}) -> ${after[0].id}`);
}

function main(args) {
  if (args.length === 0 || args.length % 2 !== 0) {
    console.error(`expected pairs of job definition and script, got ${args.length} arguments`);
    process.exit(1);
  }
  let failed = false;
  for (let i = 0; i < args.length; i += 2) {
    try {
      reconcile(JSON.parse(args[i]), args[i + 1]);
    } catch (err) {
      failed = true;
      console.error(String(err.message || err));
    }
  }
  process.exit(failed ? 1 : 0);
}

if (process.env.RECONCILE_TEST) module.exports = { quietHoursCheck, scheduleProblems, expected, ACTIVE_HOURS };
else main(process.argv.slice(1));
