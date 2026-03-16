/**
 * End-to-end test for the operator dashboard workflow.
 *
 * Validates the full journey: raise alerts via the workload API,
 * verify they appear in the Grafana dashboards, drill down into logs,
 * navigate back, clear alerts, and verify they disappear.
 *
 * Takes screenshots at each critical step and saves them to e2e-results/.
 *
 * This is distinct from the load test — it tests dashboard functionality,
 * not pipeline throughput.
 */

import { chromium } from "playwright";
import http from "http";
import { mkdirSync } from "fs";

const GRAFANA_URL = process.env.GRAFANA_URL || "http://localhost:3000";
const DASHBOARD_URL = process.env.DASHBOARD_URL || "http://localhost:8090";
const APP_BASE = process.env.APP_BASE || "http://localhost";
const OUTPUT_DIR = "e2e-results";

// APAC app ports: 8081, 8082, 8083
const APAC_APP_PORT = 8081;

// How long to wait for metrics to propagate through the pipeline
const PIPELINE_SETTLE_MS = 15_000;
// How long to wait for Grafana panels to render
const PANEL_RENDER_MS = 5_000;

mkdirSync(OUTPUT_DIR, { recursive: true });

let screenshotIndex = 0;

/**
 * Take a full-page screenshot and save it with a sequential prefix.
 */
async function screenshot(page, label) {
  screenshotIndex++;
  const name = `${String(screenshotIndex).padStart(2, "0")}-${label}.png`;
  const path = `${OUTPUT_DIR}/${name}`;
  await page.screenshot({ path, fullPage: true });
  console.log(`  Screenshot: ${path}`);
}

/**
 * Dismiss Grafana notification banners that overlay panels.
 */
async function dismissBanners(page) {
  try {
    const closeButtons = page.locator('[aria-label="Close"]');
    const count = await closeButtons.count();
    for (let i = 0; i < count; i++) {
      await closeButtons.nth(i).click().catch(() => {});
    }
  } catch {
    // No banners
  }
  await page.waitForTimeout(500);
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
      path: parsed.pathname,
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
console.log("=== E2E Dashboard Test ===\n");

// Step 1: Raise two distinct alerts on the same APAC app
console.log("Step 1: Raising alerts via workload API...");

const alert1 = await apiRequest(
  `${APP_BASE}:${APAC_APP_PORT}/raise`,
  "POST",
  {
    alert_name: "HighLatency",
    severity: "critical",
    reason: "e2e-test",
    message: "Latency above threshold",
    correlation_id: "e2e-001",
  }
);
assert(alert1.status === 201, "Raise HighLatency alert returns 201");
console.log(`  Alert 1 ID: ${alert1.body.alert_id}`);

const alert2 = await apiRequest(
  `${APP_BASE}:${APAC_APP_PORT}/raise`,
  "POST",
  {
    alert_name: "DiskPressure",
    severity: "warning",
    reason: "e2e-test",
    message: "Disk usage above 90%",
    correlation_id: "e2e-002",
  }
);
assert(alert2.status === 201, "Raise DiskPressure alert returns 201");
assert(
  alert1.body.alert_id !== alert2.body.alert_id,
  "Two different alert names produce different alert IDs"
);
console.log(`  Alert 2 ID: ${alert2.body.alert_id}`);

// Verify both appear in the app's active alerts
const activeAlerts = await apiRequest(
  `${APP_BASE}:${APAC_APP_PORT}/alerts`,
  "GET"
);
const activeIds = Object.keys(activeAlerts.body);
assert(
  activeIds.includes(alert1.body.alert_id),
  "HighLatency alert is in active alerts"
);
assert(
  activeIds.includes(alert2.body.alert_id),
  "DiskPressure alert is in active alerts"
);

// Step 2: Wait for metrics to propagate
console.log(
  `\nStep 2: Waiting ${PIPELINE_SETTLE_MS / 1000}s for pipeline to settle...`
);
await new Promise((r) => setTimeout(r, PIPELINE_SETTLE_MS));

// Step 3: Launch browser and verify dashboards
console.log("\nStep 3: Launching Playwright browser...");
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
});

// --- Regional Service Health Dashboard ---
console.log("\nStep 4: Checking Regional Service Health dashboard...");
const healthPage = await context.newPage();
const healthUrl = `${GRAFANA_URL}/d/regional-service-health?var-region=apac&var-alert_name=All&from=now-5m&to=now&refresh=off`;
await healthPage.goto(healthUrl, { waitUntil: "networkidle" });
await healthPage.waitForTimeout(PANEL_RENDER_MS);
await dismissBanners(healthPage);

await screenshot(healthPage, "service-health-firing-alerts");

// Check that the "Firing Alerts" table panel exists
const firingPanel = healthPage.locator(
  '[data-testid="data-testid Panel header Firing Alerts"]'
);
const firingPanelAlt = healthPage.locator("text=Firing Alerts").first();
const hasFiringPanel =
  (await firingPanel.count()) > 0 || (await firingPanelAlt.count()) > 0;
assert(hasFiringPanel, "Firing Alerts panel is visible");

// Check that both alert names appear in the page content
const pageText = await healthPage.textContent("body");
const hasHighLatency = pageText.includes("HighLatency");
const hasDiskPressure = pageText.includes("DiskPressure");
assert(
  hasHighLatency,
  "HighLatency alert visible in Firing Alerts table"
);
assert(
  hasDiskPressure,
  "DiskPressure alert visible in Firing Alerts table"
);

// Check the Alert Event Logs panel exists
const logsPanel = healthPage.locator("text=Alert Event Logs").first();
const hasLogsPanel = (await logsPanel.count()) > 0;
assert(hasLogsPanel, "Alert Event Logs panel is visible in service health dashboard");

// Scroll down to ensure logs panel is in view
await healthPage.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
await healthPage.waitForTimeout(3000);

await screenshot(healthPage, "service-health-logs-panel");

const logLines = healthPage.locator('[data-testid="logRows"] [data-testid="logRow"]');
const logLinesAlt = healthPage.locator(".logs-row");
const logLineCount = (await logLines.count()) + (await logLinesAlt.count());

const bodyTextAfterScroll = await healthPage.textContent("body");
const hasAlertLogContent =
  bodyTextAfterScroll.includes("Alert raised") ||
  bodyTextAfterScroll.includes("alert_name") ||
  logLineCount > 0;
assert(
  hasAlertLogContent,
  "Log content is present in the Alert Event Logs panel"
);

// Step 5: Drill down — click "View Logs" data link
console.log("\nStep 5: Testing drill-down to Alert Context logs...");

const tableCells = healthPage.locator("table td");
const cellCount = await tableCells.count();

let drillDownWorked = false;
if (cellCount > 0) {
  await tableCells.first().click();
  await healthPage.waitForTimeout(1000);

  await screenshot(healthPage, "data-link-tooltip");

  const viewLogsLink = healthPage.locator('a:has-text("View Logs")');
  const viewLogsCount = await viewLogsLink.count();

  if (viewLogsCount > 0) {
    const [newPage] = await Promise.all([
      context.waitForEvent("page", { timeout: 5000 }).catch(() => null),
      viewLogsLink.first().click(),
    ]);

    if (newPage) {
      await newPage.waitForLoadState("networkidle");
      await newPage.waitForTimeout(PANEL_RENDER_MS);

      await screenshot(newPage, "alert-context-drilldown");

      const logsPageText = await newPage.textContent("body");
      const hasLogsDashboard = logsPageText.includes("Alert Event Logs");
      assert(hasLogsDashboard, "Alert Context logs dashboard opened via drill-down");

      const backLink = newPage.locator('a:has-text("Back to Service Health")');
      const hasBackLink = (await backLink.count()) > 0;
      assert(hasBackLink, "Back to Service Health link is present (close drill-down)");

      if (hasBackLink) {
        await backLink.first().click();
        await newPage.waitForLoadState("networkidle");
        await newPage.waitForTimeout(2000);

        await screenshot(newPage, "back-to-service-health");

        const backText = await newPage.textContent("body");
        assert(
          backText.includes("Firing Alerts"),
          "Back link returns to Regional Service Health dashboard"
        );
      }

      await newPage.close();
      drillDownWorked = true;
    }
  }
}

if (!drillDownWorked) {
  console.log("  Data link click did not open popup — testing direct navigation...");
  const logsPage = await context.newPage();
  await logsPage.goto(
    `${GRAFANA_URL}/d/alert-context-logs?var-region=apac&var-alert_name=HighLatency&from=now-5m&to=now&refresh=off`,
    { waitUntil: "networkidle" }
  );
  await logsPage.waitForTimeout(PANEL_RENDER_MS);

  await screenshot(logsPage, "alert-context-direct");

  const logsPageText = await logsPage.textContent("body");
  assert(
    logsPageText.includes("Alert Event Logs"),
    "Alert Context logs dashboard loads correctly"
  );

  const backLink = logsPage.locator('a:has-text("Back to Service Health")');
  const hasBackLink = (await backLink.count()) > 0;
  assert(hasBackLink, "Back to Service Health link is present (close drill-down)");

  await logsPage.close();
}

// Step 6: Filter by specific alert name
console.log("\nStep 6: Testing alert name filter...");
const filteredPage = await context.newPage();
const filteredUrl = `${GRAFANA_URL}/d/regional-service-health?var-region=apac&var-alert_name=HighLatency&from=now-5m&to=now&refresh=off`;
await filteredPage.goto(filteredUrl, { waitUntil: "networkidle" });
await filteredPage.waitForTimeout(PANEL_RENDER_MS);
await dismissBanners(filteredPage);

await screenshot(filteredPage, "filtered-by-highlatency");

const filteredText = await filteredPage.textContent("body");
assert(
  filteredText.includes("HighLatency"),
  "Filtered view shows HighLatency alert"
);
await filteredPage.close();

// Step 7: Global Alert Overview
console.log("\nStep 7: Checking Global Alert Overview dashboard...");
const globalPage = await context.newPage();
await globalPage.goto(
  `${GRAFANA_URL}/d/global-alert-overview?from=now-5m&to=now&refresh=off`,
  { waitUntil: "networkidle" }
);
await globalPage.waitForTimeout(PANEL_RENDER_MS);
await dismissBanners(globalPage);

await screenshot(globalPage, "global-overview-with-alerts");

const globalText = await globalPage.textContent("body");
const hasAPACStat =
  globalText.includes("APAC") && !globalText.includes("No data");
assert(hasAPACStat, "Global overview shows APAC region with data");
await globalPage.close();

// Step 8: Operator Dashboard (Datastar SSE)
console.log("\nStep 8: Checking Operator Dashboard (Datastar SSE)...");

// Wait for alerts to arrive through the full pipeline:
// workload → OTEL → VictoriaMetrics → vmalert → Alertmanager → webhook → dashboard DB
// Poll the JSON API until at least one firing alert is present.
const apiPollUrl = `${DASHBOARD_URL}/api/alerts?status=firing`;
const pollStart = Date.now();
const pollTimeout = 120_000; // 2 minutes
let apiAlerts = [];
console.log("  Waiting for alerts to arrive via webhook pipeline...");
while (Date.now() - pollStart < pollTimeout) {
  try {
    const resp = await fetch(apiPollUrl);
    if (resp.ok) {
      apiAlerts = await resp.json();
      if (Array.isArray(apiAlerts) && apiAlerts.length > 0) {
        console.log(`  Found ${apiAlerts.length} firing alert(s) after ${Math.round((Date.now() - pollStart) / 1000)}s`);
        break;
      }
    }
  } catch (_) {
    // Dashboard may not be ready yet
  }
  await new Promise((r) => setTimeout(r, 3000));
}

const opPage = await context.newPage();
await opPage.goto(DASHBOARD_URL, { waitUntil: "domcontentloaded" });
// Give SSE feed time to deliver the initial fragments
await opPage.waitForTimeout(5000);

await screenshot(opPage, "operator-dashboard-live");

const opText = await opPage.textContent("body");
assert(opText.includes("Alert Dashboard"), "Operator dashboard loaded");

// Alerts should have arrived via Alertmanager webhook → SSE broadcast
const opHasAlerts =
  opText.includes("HighLatency") || opText.includes("DiskPressure");
assert(opHasAlerts, "Operator dashboard shows alerts received via SSE");

// Check stat pills rendered
const hasPills =
  opText.includes("critical") && opText.includes("warning");
assert(hasPills, "Operator dashboard stat pills rendered");

// Click on an alert card to open the detail panel
const alertCards = opPage.locator(".alert-card");
const cardCount = await alertCards.count();
if (cardCount > 0) {
  await alertCards.first().click();
  await opPage.waitForTimeout(2000);

  await screenshot(opPage, "operator-detail-panel");

  const detailText = await opPage.textContent("body");
  // Detail panel should show labels, history, and the Load logs button
  const hasDetail =
    detailText.includes("Details") &&
    detailText.includes("Labels") &&
    detailText.includes("Load logs");
  assert(hasDetail, "Operator detail panel shows alert context");

  // Click Load logs
  const loadLogsBtn = opPage.locator('button:has-text("Load logs")');
  if ((await loadLogsBtn.count()) > 0) {
    await loadLogsBtn.first().click();
    await opPage.waitForTimeout(3000);

    await screenshot(opPage, "operator-logs-loaded");

    const logText = await opPage.locator("#log-container").textContent();
    // Should show log entries or at minimum no error
    const logsLoaded =
      logText.includes("log-line") ||
      logText.includes("Alert") ||
      !logText.includes("Failed to load");
    assert(logsLoaded, "Operator Load logs returns without error");
  }

  // Close the detail panel via Back button
  const backBtn = opPage.locator('button:has-text("Back to list")');
  if ((await backBtn.count()) > 0) {
    await backBtn.first().click();
    await opPage.waitForTimeout(1000);

    await screenshot(opPage, "operator-panel-closed");

    // Detail panel should be hidden (selectedId reset to 0)
    const panelVisible = await opPage
      .locator("#detail-panel")
      .isVisible()
      .catch(() => true);
    assert(!panelVisible, "Operator detail panel closed via Back button");
  }
} else {
  console.log("  No alert cards found — webhook may not have arrived yet");
}

await opPage.close();

// Step 9: Close alerts via the operator dashboard (routes through Close Relay)
console.log("\nStep 9: Closing alerts via dashboard Close Relay...");

// Find firing alerts in the dashboard DB
const firingResp = await apiRequest(`${DASHBOARD_URL}/api/alerts?status=firing`, "GET");
const firingAlerts = Array.isArray(firingResp.body) ? firingResp.body : [];
console.log(`  Found ${firingAlerts.length} firing alert(s) in dashboard`);

// Close each via the dashboard API (which calls the regional Close Relay)
for (const a of firingAlerts) {
  const closeResp = await apiRequest(`${DASHBOARD_URL}/alerts/${a.id}/close`, "POST");
  console.log(`  Closed alert ${a.alert_name} (db_id=${a.id}): ${closeResp.status}`);
}
assert(firingAlerts.length > 0, "Had firing alerts to close via Close Relay");

// Poll until the dashboard shows no firing alerts
console.log("  Waiting for Close Relay to resolve alerts...");
const closeStart = Date.now();
let firingCount = firingAlerts.length;
while (Date.now() - closeStart < 60_000) {
  const resp = await apiRequest(`${DASHBOARD_URL}/api/alerts?status=firing`, "GET");
  firingCount = Array.isArray(resp.body) ? resp.body.length : 0;
  if (firingCount === 0) break;
  await new Promise((r) => setTimeout(r, 3000));
}
assert(firingCount === 0, "All alerts resolved after Close Relay close");

// Clean up workload internal state (best-effort, not part of the close flow)
await apiRequest(`${APP_BASE}:${APAC_APP_PORT}/clear`, "POST", { alert_id: alert1.body.alert_id });
await apiRequest(`${APP_BASE}:${APAC_APP_PORT}/clear`, "POST", { alert_id: alert2.body.alert_id });

// Wait for resolved state to propagate to Grafana
console.log(
  `\nStep 10: Waiting ${PIPELINE_SETTLE_MS / 1000}s for resolved state to propagate...`
);
await new Promise((r) => setTimeout(r, PIPELINE_SETTLE_MS));

// Check Grafana shows no firing alerts
const verifyPage = await context.newPage();
await verifyPage.goto(
  `${GRAFANA_URL}/d/regional-service-health?var-region=apac&var-alert_name=All&from=now-5m&to=now&refresh=off`,
  { waitUntil: "networkidle" }
);
await verifyPage.waitForTimeout(PANEL_RENDER_MS);
await dismissBanners(verifyPage);

await screenshot(verifyPage, "after-clear-no-firing");

const verifyText = await verifyPage.textContent("body");
const alertsCleared =
  !verifyText.includes("FIRING") || verifyText.includes("No data");
assert(alertsCleared, "Grafana shows no firing alerts after Close Relay close");
await verifyPage.close();

// Cleanup
await browser.close();

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
