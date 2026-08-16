import { test, expect, Page } from "@playwright/test";
import path from "path";

const csvA = path.resolve(__dirname, "../../fixtures/csv/portfolio_a.csv");

// Relative luminance + WCAG contrast ratio from "rgb(r, g, b)" strings.
function parseRgb(s: string): [number, number, number] | null {
  const m = s.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function luminance([r, g, b]: [number, number, number]): number {
  const f = (c: number) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrast(fg: string, bg: string): number | null {
  const a = parseRgb(fg);
  const b = parseRgb(bg);
  if (!a || !b) return null;
  const l1 = luminance(a);
  const l2 = luminance(b);
  const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

// On narrow viewports the expanded sidebar overlays main content and swallows clicks.
async function collapseSidebarIfOverlaying(page: Page) {
  const viewport = page.viewportSize();
  if (!viewport || viewport.width > 900) return;
  const collapse = page.locator('[data-testid="stSidebarCollapseButton"] button');
  if (await collapse.count()) {
    await collapse.first().click({ timeout: 5_000 }).catch(() => undefined);
    await page.waitForTimeout(500);
  }
}

async function acceptDisclaimer(page: Page) {
  await expect(page.getByRole("heading", { name: /^Disclaimer$/i })).toBeVisible({
    timeout: 60_000,
  });
  await collapseSidebarIfOverlaying(page);
  const agreeBtn = page.getByRole("button", { name: /continue/i });
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

// theme.base is process-wide, so a previous test can leave the toggle already on.
// Drive it to an explicit state instead of assuming a click enables dark mode.
async function setDarkMode(page: Page, enabled: boolean) {
  const targetIcon = enabled ? "🌙" : "☀️";
  const target = page.getByRole("button", { name: targetIcon });
  if (await target.isVisible()) {
    await target.click();
    // A theme change costs two runs: one to set config, one to deliver it.
    await page.waitForTimeout(4000);
  }
  const activeIcon = enabled ? "☀️" : "🌙";
  await expect(page.getByRole("button", { name: activeIcon })).toBeVisible({
    timeout: 15_000,
  });
}

// Elements that must adapt between themes.
const PROBES: Array<{ name: string; selector: string }> = [
  { name: "app-container", selector: '[data-testid="stAppViewContainer"]' },
  { name: "main", selector: '[data-testid="stMain"]' },
  { name: "header", selector: '[data-testid="stHeader"]' },
  { name: "tabs", selector: '[data-testid="stTabs"]' },
  { name: "expander", selector: '[data-testid="stExpander"] details' },
  { name: "dataframe", selector: '[data-testid="stDataFrameResizable"]' },
  { name: "metric", selector: '[data-testid="stMetric"]' },
  {
    name: "selectbox",
    selector: '[data-testid="stSelectbox"], [data-testid="stSelectboxVirtual"], div[data-baseweb="select"]',
  },
  {
    name: "download-button",
    selector: '[data-testid="stDownloadButton"] button',
  },
  { name: "expander-header", selector: '[data-testid="stExpander"] summary' },
];

// Dataframes are painted on a <canvas>, so CSS cannot theme them. Sampling real
// pixels is the only way to prove the grid itself repainted for the new theme.
async function sampleGridPixel(page: Page): Promise<string> {
  return page.evaluate(() => {
    const canvas = document.querySelector<HTMLCanvasElement>(
      '[data-testid="stDataFrame"] canvas'
    );
    if (!canvas) return "none";
    const ctx = canvas.getContext("2d");
    if (!ctx) return "none";
    // Sample inside the grid body, away from borders.
    const d = ctx.getImageData(30, 30, 1, 1).data;
    return `rgb(${d[0]}, ${d[1]}, ${d[2]})`;
  });
}

async function probeColors(page: Page) {
  return page.evaluate((probes) => {
    const out: Record<string, { bg: string; fg: string; found: boolean }> = {};
    // Composite the element's background over its ancestors so translucent
    // overlays report the color a user actually sees.
    const resolveBg = (el: Element): string => {
      const layers: Array<[number, number, number, number]> = [];
      let cur: Element | null = el;
      while (cur) {
        const bg = getComputedStyle(cur).backgroundColor;
        const m = bg.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
        if (m) {
          const alpha = m[4] === undefined ? 1 : Number(m[4]);
          if (alpha > 0) {
            layers.push([Number(m[1]), Number(m[2]), Number(m[3]), alpha]);
            if (alpha === 1) break;
          }
        }
        cur = cur.parentElement;
      }
      if (!layers.length) return "rgb(255, 255, 255)";
      // Start from the deepest opaque layer and composite upward.
      let [r, g, b] = layers[layers.length - 1].slice(0, 3) as [number, number, number];
      for (let i = layers.length - 2; i >= 0; i--) {
        const [lr, lg, lb, la] = layers[i];
        r = lr * la + r * (1 - la);
        g = lg * la + g * (1 - la);
        b = lb * la + b * (1 - la);
      }
      return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
    };
    for (const p of probes) {
      const el = document.querySelector(p.selector);
      if (!el) {
        out[p.name] = { bg: "", fg: "", found: false };
        continue;
      }
      const cs = getComputedStyle(el);
      out[p.name] = { bg: resolveBg(el), fg: cs.color, found: true };
    }
    return out;
  }, PROBES);
}

test.describe("Theme adaptation audit", () => {
  test("all key elements adapt to dark mode with readable contrast", async ({
    page,
  }, testInfo) => {
    await page.goto("/");
    await acceptDisclaimer(page);

    await page.locator('input[type="file"]').setInputFiles([csvA]);
    await page.getByRole("button", { name: /run analysis/i }).click();
    await expect(page.getByRole("tab", { name: "Ticker Details" })).toBeVisible({
      timeout: 120_000,
    });

    // Grids paint asynchronously; wait so probes see final colors.
    await expect(page.locator('[data-testid="stDataFrame"]').first()).toBeVisible({
      timeout: 60_000,
    });
    await setDarkMode(page, false);
    await page.waitForTimeout(1500);

    const light = await probeColors(page);
    const lightGrid = await sampleGridPixel(page);
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/theme/${testInfo.project.name}/light.png`),
      fullPage: true,
    });

    await setDarkMode(page, true);
    await expect(page.getByRole("tab", { name: "Ticker Details" })).toBeVisible({
      timeout: 120_000,
    });
    const viewport = test.info().project.name;
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/e2e/${viewport}/theme-dark.png`),
      fullPage: true,
    });
    await expect(page.locator('[data-testid="stDataFrame"]').first()).toBeVisible({
      timeout: 60_000,
    });
    await page.waitForTimeout(1500);
    const dark = await probeColors(page);
    const darkGrid = await sampleGridPixel(page);
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/theme/${testInfo.project.name}/dark.png`),
      fullPage: true,
    });

    const failures: string[] = [];

    console.log(`grid-canvas        light=${lightGrid} dark=${darkGrid}`);
    if (lightGrid === "none" || darkGrid === "none") {
      failures.push("dataframe canvas: could not sample pixels");
    } else {
      const gridLum = luminance(parseRgb(darkGrid)!);
      if (gridLum > 0.5)
        failures.push(`dataframe canvas: grid still light in dark mode (${darkGrid})`);
      if (lightGrid === darkGrid)
        failures.push(`dataframe canvas: did not repaint (both ${lightGrid})`);
    }

    for (const probe of PROBES) {
      const l = light[probe.name];
      const d = dark[probe.name];
      if (!l?.found || !d?.found) {
        failures.push(`${probe.name}: element not found (light=${l?.found}, dark=${d?.found})`);
        continue;
      }
      const darkBg = parseRgb(d.bg);
      const darkLum = darkBg ? luminance(darkBg) : 1;
      const ratio = contrast(d.fg, d.bg);
      const line =
        `${probe.name.padEnd(18)} light(bg=${l.bg}) dark(bg=${d.bg}, fg=${d.fg}) ` +
        `lum=${darkLum.toFixed(3)} contrast=${ratio ? ratio.toFixed(2) : "n/a"}`;
      console.log(line);

      // In dark mode a surface must actually be dark.
      if (darkLum > 0.5) failures.push(`${probe.name}: still light in dark mode (bg=${d.bg})`);
      // And text on it must be readable.
      if (ratio !== null && ratio < 4.5)
        failures.push(`${probe.name}: low contrast ${ratio.toFixed(2)} (fg=${d.fg} bg=${d.bg})`);
    }

    // Switching back must fully restore light mode, not leave dark remnants.
    await setDarkMode(page, false);
    await page.waitForTimeout(1500);
    await page.screenshot({
      path: path.resolve(__dirname, `../../artifacts/e2e/${viewport}/theme-light.png`),
      fullPage: true,
    });
    const relight = await probeColors(page);
    const relightGrid = await sampleGridPixel(page);
    console.log(`\ngrid-canvas (back to light) = ${relightGrid}`);

    if (relightGrid !== "none" && luminance(parseRgb(relightGrid)!) < 0.5) {
      failures.push(`dataframe canvas: stayed dark after switching back (${relightGrid})`);
    }
    for (const probe of PROBES) {
      const r = relight[probe.name];
      if (!r?.found) continue;
      const bg = parseRgb(r.bg);
      if (!bg) continue;
      const lum = luminance(bg);
      console.log(`${probe.name.padEnd(18)} back-to-light bg=${r.bg} lum=${lum.toFixed(3)}`);
      if (lum < 0.5) failures.push(`${probe.name}: stayed dark in light mode (bg=${r.bg})`);
      const ratio = contrast(r.fg, r.bg);
      if (ratio !== null && ratio < 4.5)
        failures.push(`${probe.name}: low contrast in light mode ${ratio.toFixed(2)}`);
    }

    console.log("\nFAILURES:\n" + (failures.length ? failures.join("\n") : "none"));
    expect(failures, `Theme adaptation issues:\n${failures.join("\n")}`).toEqual([]);
  });
});
