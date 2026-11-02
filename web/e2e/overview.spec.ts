import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import * as fs from 'node:fs';
import * as path from 'node:path';

const fixturesDir = path.join(__dirname, 'fixtures');

function fixture(name: string): unknown {
  const p = path.join(fixturesDir, name);
  if (!fs.existsSync(p)) return { ok: true };
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const u = new URL(route.request().url());
    const key = u.pathname.replace(/\//g, '_').replace(/^_/, '') + '.json';
    const body = fixture(key);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
});

test('overview renders at desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/');
  await page.waitForURL('**/app/command/overview', { timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible({ timeout: 15_000 });
});

test('overview mobile width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.waitForURL('**/app/command/overview', { timeout: 15_000 });
  await expect(page.getByText('Watchlist coins')).toBeVisible({ timeout: 15_000 });
});

test('overview passes axe smoke', async ({ page }) => {
  await page.goto('/');
  await page.waitForURL('**/app/command/overview', { timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible({ timeout: 15_000 });
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === 'critical')).toEqual([]);
});
