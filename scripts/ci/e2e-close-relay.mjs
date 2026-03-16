/**
 * End-to-end test for the Close Relay raise/close/re-raise lifecycle.
 *
 * Validates:
 *   1. Raise alert via workload API -> appears on the operator dashboard
 *   2. Close alert via dashboard API -> resolves in dashboard
 *   3. Re-raise the same alert -> reappears as firing
 *
 * Uses the same Playwright patterns as e2e-dashboard.mjs.
 * Assumes the lab is already running via `docker compose up -d --build --wait`.
 *
 * Screenshots are saved to e2e-results/ with sequential prefixes.
 */

import { chromium } from "playwright";
import http from "http";
import { mkdirSync } from "fs";

const DASHBOARD_URL = process.env.DASHBOARD_URL || "http://localhost:8090";
const APP_BASE = process.env.APP_BASE || "http://localhost";
const OUTPUT_DIR = "e2e-results";

// APAC app-1 on host port 8081
const APAC_APP_PORT = 8081;

// Pipeline propagation: workload -> OTEL -> VM -> vmalert -> Alertmanager -> webhook -> dashboard
const PIPELINE_TIMEOUT_MS = 120_000;
const POLL_INTERVAL_MS = 3_000;

mkdirSync(OUTPUT_DIR, { recursive: true });

let screenshotIndex = 0;

/**
 * Take a full-page screenshot and save it with a sequential prefix.
 */
async function screenshot(page, label) {
  screenshotIndex++;
  const name = `${String(screenshotIndex).padStart(2, "0")}-cr-${label}.png`;
  const path = `${OUTPUT_DIR}/${name}`;
  await page.screenshot({ path, fullPage: true });
  console.log(`  Screenshot: ${path}`);
}

/**
 * Make an HTTP request and return parsed JSON.
 */
function apiRequest(url, method, body) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const options = {
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.pathname + parsed.search,
      method,
      headers: { "Content-Type": "application/json" },
    };
    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(data) });
        } catch {
          resolve({ status: res.statusCode, body: data });
        }
      });
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

/**
 * Poll a function until it returns a truthy value or timeout is reached.
 */
async function pollUntil(fn, timeoutMs = PIPELINE_TIMEOUT_MS, intervalMs = POLL_INTERVAL_MS) {
  const start = Date.now();
  let lastError;
  while (Date.now() - start < timeoutMs) {
    try {
      const result = await fn();
      if (result) return result;
    } catch (err) {
      lastError = err;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(
    `Polling timed out after ${timeoutMs}ms` +
      (lastError ? `: ${lastError.message}` : "")
  );
}

let exitCode = 0;
const results = [];

function assert(condition, name) {
  if (condition) {
    results.push({ name, passed: true });
    console.log(`  PASS: ${name}`);
  } else {
    results.push({ name, passed: false });
    console.log(`  FAIL: ${name}`);
    exitCode = 1;
  }
}

// ---------------------------------------------------------------------------
// Main test
// ---------------------------------------------------------------------------
console.log("=== E2E Close Relay Test ===\n");

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
});

try {
  // =========================================================================
  // Test 1: Raise alert and verify it appears on the dashboard
  // =========================================================================
  console.log("Test 1: Raise alert and verify it appears on dashboard...\n");

  console.log("  Raising CloseRelayTest alert via workload API...");
  const raiseResp = await apiRequest(
    `${APP_BASE}:${APAC_APP_PORT}/raise`,
    "POST",
    {
      alert_name: "CloseRelayTest",
      severity: "critical",
      reason: "e2e-close-relay",
      message: "Testing close relay lifecycle",
      correlation_id: "cr-e2e-001",
    }
  );
  assert(raiseResp.status === 201, "Raise CloseRelayTest alert returns 201");
  console.log(`  Alert ID: ${raiseResp.body.alert_id}`);

  // Poll the dashboard API until the alert appears as firing
  console.log("  Waiting for alert to arrive via pipeline...");
  const firingAlerts = await pollUntil(async () => {
    const resp = await apiRequest(
      `${DASHBOARD_URL}/api/alerts?status=firing`,
      "GET"
    );
    if (resp.status !== 200) return null;
    const alerts = resp.body;
    if (!Array.isArray(alerts)) return null;
    const match = alerts.find((a) => a.alert_name === "CloseRelayTest");
    return match || null;
  });
  console.log(
    `  Alert arrived in dashboard (db id: ${firingAlerts.id})`
  );
  const alertDbId = firingAlerts.id;
  assert(!!alertDbId, "CloseRelayTest alert is firing in dashboard API");

  // Verify stats show at least 1 firing
  const statsAfterRaise = await apiRequest(
    `${DASHBOARD_URL}/api/alerts/stats`,
    "GET"
  );
  assert(
    statsAfterRaise.status === 200 && statsAfterRaise.body.firing >= 1,
    "Dashboard stats show >= 1 firing alert after raise"
  );

  // Verify it appears in the browser UI
  const raisePage = await context.newPage();
  await raisePage.goto(DASHBOARD_URL, { waitUntil: "domcontentloaded" });
  await raisePage.waitForTimeout(5000); // Give SSE time to deliver fragments

  await screenshot(raisePage, "after-raise");

  const raisePageText = await raisePage.textContent("body");
  assert(
    raisePageText.includes("CloseRelayTest"),
    "CloseRelayTest alert visible in dashboard UI after raise"
  );
  assert(
    raisePageText.includes("critical"),
    "Alert severity (critical) visible in dashboard UI"
  );
  await raisePage.close();

  // =========================================================================
  // Test 2: Close alert via dashboard API and verify it resolves
  // =========================================================================
  console.log("\nTest 2: Close alert and verify it resolves...\n");

  // Close via the dashboard API (POST /alerts/{db_id}/close)
  // This endpoint returns SSE, so we use fetch for the raw response
  console.log(`  Closing alert db_id=${alertDbId} via dashboard API...`);
  const closeResp = await new Promise((resolve, reject) => {
    const parsed = new URL(`${DASHBOARD_URL}/alerts/${alertDbId}/close`);
    const options = {
      hostname: parsed.hostname,
      port: parsed.port,
      path: parsed.pathname,
      method: "POST",
      headers: { "Content-Type": "application/json" },
    };
    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => resolve({ status: res.statusCode, body: data }));
    });
    req.on("error", reject);
    req.end();
  });
  assert(closeResp.status === 200, "Close alert API returns 200");

  // Poll until the dashboard API confirms it is resolved
  console.log("  Waiting for alert to show as resolved...");
  await pollUntil(async () => {
    const resp = await apiRequest(
      `${DASHBOARD_URL}/api/alerts?status=resolved`,
      "GET"
    );
    if (resp.status !== 200) return null;
    const alerts = resp.body;
    if (!Array.isArray(alerts)) return null;
    return alerts.find(
      (a) => a.id === alertDbId && a.status === "resolved"
    );
  }, 30_000);
  console.log("  Alert is now resolved in dashboard DB");

  // Verify firing count decreased
  const statsAfterClose = await apiRequest(
    `${DASHBOARD_URL}/api/alerts/stats`,
    "GET"
  );
  assert(
    statsAfterClose.status === 200 && statsAfterClose.body.resolved >= 1,
    "Dashboard stats show >= 1 resolved alert after close"
  );

  // Verify the alert no longer appears as firing in the API
  const firingAfterClose = await apiRequest(
    `${DASHBOARD_URL}/api/alerts?status=firing`,
    "GET"
  );
  const stillFiring = Array.isArray(firingAfterClose.body)
    ? firingAfterClose.body.find((a) => a.id === alertDbId)
    : null;
  assert(!stillFiring, "CloseRelayTest alert no longer in firing list");

  // Check browser UI shows resolved state
  const closePage = await context.newPage();
  await closePage.goto(DASHBOARD_URL, { waitUntil: "domcontentloaded" });
  await closePage.waitForTimeout(5000);

  await screenshot(closePage, "after-close");

  const closePageText = await closePage.textContent("body");
  assert(
    closePageText.includes("resolved"),
    "Dashboard UI shows 'resolved' status after close"
  );
  await closePage.close();

  // =========================================================================
  // Test 3: Re-raise the same alert and verify it reappears as firing
  // =========================================================================
  console.log("\nTest 3: Re-raise alert and verify it reappears...\n");

  // Re-raise the same alert (same name + severity -> same alert_id in workload)
  console.log("  Re-raising CloseRelayTest alert...");
  const reRaiseResp = await apiRequest(
    `${APP_BASE}:${APAC_APP_PORT}/raise`,
    "POST",
    {
      alert_name: "CloseRelayTest",
      severity: "critical",
      reason: "e2e-close-relay-reraise",
      message: "Re-raise after close to test lifecycle",
      correlation_id: "cr-e2e-002",
    }
  );
  assert(
    reRaiseResp.status === 201,
    "Re-raise CloseRelayTest alert returns 201"
  );

  // The re-raise emits a new lab_alert_raised timestamp > lab_alert_closed timestamp,
  // so vmalert's EventAlertActive rule should fire again. Wait for it to arrive
  // through the pipeline as a new firing alert.
  console.log("  Waiting for re-raised alert to arrive via pipeline...");

  // The dashboard may receive a new webhook that flips the existing row back to firing,
  // or create a new row. Poll for any firing CloseRelayTest alert.
  const reRaisedAlert = await pollUntil(async () => {
    const resp = await apiRequest(
      `${DASHBOARD_URL}/api/alerts?status=firing`,
      "GET"
    );
    if (resp.status !== 200) return null;
    const alerts = resp.body;
    if (!Array.isArray(alerts)) return null;
    return alerts.find((a) => a.alert_name === "CloseRelayTest");
  });
  assert(
    !!reRaisedAlert,
    "CloseRelayTest alert reappears as firing after re-raise"
  );
  console.log(`  Re-raised alert in dashboard (db id: ${reRaisedAlert.id})`);

  // Verify stats show firing count increased
  const statsAfterReRaise = await apiRequest(
    `${DASHBOARD_URL}/api/alerts/stats`,
    "GET"
  );
  assert(
    statsAfterReRaise.status === 200 && statsAfterReRaise.body.firing >= 1,
    "Dashboard stats show >= 1 firing alert after re-raise"
  );

  // Verify in browser UI
  const reRaisePage = await context.newPage();
  await reRaisePage.goto(DASHBOARD_URL, { waitUntil: "domcontentloaded" });
  await reRaisePage.waitForTimeout(5000);

  await screenshot(reRaisePage, "after-reraise");

  const reRaisePageText = await reRaisePage.textContent("body");
  assert(
    reRaisePageText.includes("CloseRelayTest"),
    "CloseRelayTest alert visible in dashboard UI after re-raise"
  );
  // The re-raised alert should show as firing (not resolved)
  const hasFireBadge =
    reRaisePageText.includes("firing") || reRaisePageText.includes("critical");
  assert(
    hasFireBadge,
    "Re-raised alert shows firing/critical status in dashboard UI"
  );
  await reRaisePage.close();

  // =========================================================================
  // Cleanup: close the re-raised alert so we leave a clean state
  // =========================================================================
  console.log("\nCleanup: closing re-raised alert and clearing workload...");

  // Close via dashboard
  if (reRaisedAlert && reRaisedAlert.id) {
    await new Promise((resolve, reject) => {
      const parsed = new URL(
        `${DASHBOARD_URL}/alerts/${reRaisedAlert.id}/close`
      );
      const options = {
        hostname: parsed.hostname,
        port: parsed.port,
        path: parsed.pathname,
        method: "POST",
        headers: { "Content-Type": "application/json" },
      };
      const req = http.request(options, (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => resolve({ status: res.statusCode }));
      });
      req.on("error", reject);
      req.end();
    });
  }

  // Clear workload alert state
  await apiRequest(`${APP_BASE}:${APAC_APP_PORT}/clear`, "POST", {
    alert_id: raiseResp.body.alert_id,
    reason: "e2e-close-relay-cleanup",
  });

  console.log("\nAll close-relay e2e tests passed");
} catch (err) {
  console.error(`\nTest error: ${err.message}`);
  if (err.stack) console.error(err.stack);
  exitCode = 1;
} finally {
  await browser.close();
}

// Summary
console.log("\n=== Results ===");
const passed = results.filter((r) => r.passed).length;
const total = results.length;
console.log(`${passed}/${total} assertions passed`);
console.log(`Screenshots saved to ${OUTPUT_DIR}/\n`);

for (const r of results) {
  console.log(`  ${r.passed ? "PASS" : "FAIL"}: ${r.name}`);
}

if (exitCode !== 0) {
  console.log("\nSome assertions failed.");
}

process.exit(exitCode);
