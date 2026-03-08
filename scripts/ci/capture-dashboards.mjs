/**
 * Capture Grafana dashboard screenshots using Playwright.
 *
 * Connects to the local Grafana instance, navigates to each dashboard,
 * waits for panels to render, and saves full-page screenshots.
 */

import { chromium } from "playwright";
import { mkdirSync } from "fs";

const GRAFANA_URL = "http://localhost:3000";
const OUTPUT_DIR = "perf-results";

const DASHBOARDS = [
  {
    uid: "global-alert-overview",
    name: "global-alert-overview",
    title: "Global Alert Overview",
    // Show last 15 minutes to capture the full load test window
    params: "from=now-15m&to=now&refresh=off",
  },
  {
    uid: "regional-service-health",
    name: "regional-service-health-apac",
    title: "Regional Service Health (APAC)",
    params: "from=now-15m&to=now&refresh=off&var-region=apac",
  },
  {
    uid: "regional-service-health",
    name: "regional-service-health-eu",
    title: "Regional Service Health (EU)",
    params: "from=now-15m&to=now&refresh=off&var-region=eu",
  },
  {
    uid: "regional-service-health",
    name: "regional-service-health-us",
    title: "Regional Service Health (US)",
    params: "from=now-15m&to=now&refresh=off&var-region=us",
  },
];

mkdirSync(OUTPUT_DIR, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  // Grafana has anonymous auth enabled with Admin role
});

for (const dash of DASHBOARDS) {
  const page = await context.newPage();
  const url = `${GRAFANA_URL}/d/${dash.uid}?${dash.params}&kiosk`;

  console.log(`Capturing ${dash.title} ...`);
  await page.goto(url, { waitUntil: "networkidle" });

  // Wait for panels to finish loading (Grafana renders loading spinners)
  await page.waitForTimeout(5000);

  // Dismiss any notification banners that might overlay panels
  try {
    const closeButtons = page.locator('[aria-label="Close"]');
    const count = await closeButtons.count();
    for (let i = 0; i < count; i++) {
      await closeButtons.nth(i).click().catch(() => {});
    }
  } catch {
    // No banners to dismiss
  }

  await page.waitForTimeout(1000);

  const path = `${OUTPUT_DIR}/${dash.name}.png`;
  await page.screenshot({ path, fullPage: true });
  console.log(`  Saved: ${path}`);
  await page.close();
}

await browser.close();
console.log("Dashboard capture complete.");
