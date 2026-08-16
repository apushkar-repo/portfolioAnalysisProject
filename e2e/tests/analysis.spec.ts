import { test, expect } from "@playwright/test";
import path from "path";

const csvA = path.resolve(__dirname, "../../fixtures/csv/portfolio_a.csv");
const csvB = path.resolve(__dirname, "../../fixtures/csv/portfolio_b.csv");

async function acceptDisclaimer(page: import("@playwright/test").Page) {
  await expect(page.getByRole("heading", { name: /^Disclaimer$/i })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByText(/informational and educational purposes only/i)).toBeVisible();

  // On narrow viewports the expanded sidebar overlays main content.
  const viewport = page.viewportSize();
  if (viewport && viewport.width <= 900) {
    const collapse = page.locator('[data-testid="stSidebarCollapseButton"] button');
    if (await collapse.count()) {
      await collapse.first().click({ timeout: 5_000 }).catch(() => undefined);
      await page.waitForTimeout(500);
    }
  }

  const agreeBtn = page.getByRole("button", { name: /continue/i });
  await expect(agreeBtn).toBeDisabled();

  // Scope to main content — sidebar may contain a hidden toggle/checkbox.
  const checkbox = page
    .locator('[data-testid="stMain"]')
    .getByRole("checkbox", { name: /I have read this disclaimer/i });
  await checkbox.click({ force: true });
  await expect(agreeBtn).toBeEnabled({ timeout: 20_000 });
  await agreeBtn.click();

  await expect(page.getByRole("button", { name: /run analysis/i })).toBeVisible({
    timeout: 30_000,
  });
}

test.describe("Portfolio analysis flow", () => {
  test("upload CSVs and navigate result tabs", async ({ page }, testInfo) => {
    await page.goto("/");

    await acceptDisclaimer(page);

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles(csvA);

    const runBtn = page.getByRole("button", { name: /run analysis/i });
    await expect(runBtn).toBeEnabled({ timeout: 30_000 });
    await runBtn.click();

    await expect(page.getByRole("tab", { name: "Data Overview" })).toBeVisible({
      timeout: 120_000,
    });
    // Wait for tab content, not just the tab strip: the strip appears while the
    // rerun is still painting the previous screen.
    await expect(page.getByText(/Positions by what you paid/i)).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText(/Today’s summary · YTD/i)).toBeVisible();
    await expect(
      page.getByText(/Biggest contributor: [A-Z.]+ \(\+\$[\d,]+\.\d{2}\)/)
    ).toBeVisible();
    await expect(page.getByText(/Diversification health:/i)).toBeVisible();
    await expect(page.getByText("Show holdings")).toBeVisible();
    await expect(page.getByText(/Portfolio investment over time/i)).toBeVisible();

    // A Vega field name containing "." resolves to nothing and silently draws an
    // empty chart, so assert the pie really produced slices and a populated
    // legend rather than only checking that its heading exists.
    const pie = page
      .locator('[data-testid="stVegaLiteChart"]')
      .filter({ hasText: "AAPL" })
      .first();
    await expect(pie).toBeVisible({ timeout: 30_000 });
    expect(await pie.locator("svg path").count()).toBeGreaterThan(6);

    await page.getByText("Add more portfolio files", { exact: true }).click();
    const addFilesPanel = page.locator(".st-key-additional_csv_uploads_0");
    await addFilesPanel.locator('input[type="file"]').setInputFiles(csvB);
    const updateButton = page.getByRole("button", {
      name: "Add files and update analysis",
    });
    await expect(updateButton).toBeEnabled();
    await updateButton.click();

    const filesMetric = page
      .locator('[data-testid="stMetric"]')
      .filter({ hasText: "Files uploaded" });
    await expect(filesMetric).toContainText("2", { timeout: 120_000 });
    // The second file used to be asserted via the source pie legend, which was
    // replaced by the portfolio-level investment chart. Use the source count.
    await expect(
      page.locator('[data-testid="stMetric"]').filter({ hasText: "Unique sources" })
    ).toContainText("2");

    const appHeader = page.locator(".st-key-app_header");
    await expect(
      appHeader.getByRole("button", { name: "New analysis" })
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Download PDF report" })).toHaveCount(1);
    await expect(appHeader).toBeVisible();

    // Scroll rather than trust `position: sticky`: the header only truly pins if
    // its wrapper is the sticky element and Streamlit's own top bar is cleared.
    const before = await appHeader.boundingBox();
    await page.evaluate(() => {
      const main = document.querySelector('[data-testid="stMain"]') as HTMLElement;
      if (main && main.scrollHeight > main.clientHeight) main.scrollTop = 600;
      else window.scrollTo(0, 600);
    });
    await page.waitForTimeout(600);
    const after = await appHeader.boundingBox();
    expect(after).not.toBeNull();
    // Still fully on screen, and not hidden under Streamlit's 60px top bar.
    expect(after!.y).toBeGreaterThanOrEqual(59);
    expect(after!.y).toBeLessThanOrEqual(before!.y);
    await expect(
      appHeader.getByRole("button", { name: "New analysis" })
    ).toBeInViewport();
    // The header must be opaque so scrolled content cannot show through it.
    const headerBg = await appHeader.evaluate(
      (el) => getComputedStyle(el.parentElement as HTMLElement).backgroundColor
    );
    expect(headerBg).not.toBe("rgba(0, 0, 0, 0)");
    await page.evaluate(() => {
      const main = document.querySelector('[data-testid="stMain"]') as HTMLElement;
      if (main) main.scrollTop = 0;
      else window.scrollTo(0, 0);
    });
    await page.waitForTimeout(400);
    await page.waitForTimeout(1_500);
    const viewport = testInfo.project.name;
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/e2e/${viewport}/data-overview.png`),
      fullPage: true,
    });

    await page.getByRole("tab", { name: "Performance" }).click();
    await expect(page.getByText("Portfolio value", { exact: true })).toBeVisible();
    await expect(page.getByText(/What drove the change over YTD/i)).toBeVisible();
    await expect(page.getByText("Top detractors", { exact: true })).toBeVisible();
    await expect(page.getByText(/Portfolio return trend by interval/i)).toBeVisible();
    await expect(page.getByText(/Source and industry comparison · YTD/i)).toBeVisible();
    await expect(page.getByText("Net return by industry")).toBeVisible();
    await expect(page.getByText(/Things you may want to review/i)).toBeVisible();
    await page.waitForTimeout(1_500);
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/e2e/${viewport}/performance.png`),
      fullPage: true,
    });

    await page.getByRole("tab", { name: "Ticker Details" }).click();
    const currentPrice = page
      .locator('[data-testid="stMetric"]')
      .filter({ hasText: "Current price" });
    await expect(currentPrice).toBeVisible();
    await expect(currentPrice).toContainText(/\$[\d,]+\.\d{2}/);
    await expect(page.getByText(/^When you bought AAPL/i)).toBeVisible();
    await expect(page.getByText(/^AAPL return components by interval/i)).toBeVisible();
    await page.waitForTimeout(1_500);
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/e2e/${viewport}/ticker-details.png`),
      fullPage: true,
    });

    await page.getByRole("tab", { name: "Ask Pulse" }).click();
    await expect(page.getByRole("heading", { name: "Ask Pulse" })).toBeVisible();
    await expect(
      page.getByText(/Ask questions about this analysis in plain language/i)
    ).toBeVisible();
  });
});
