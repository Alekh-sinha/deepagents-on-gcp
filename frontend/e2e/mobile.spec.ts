import { expect, test } from "@playwright/test";
import { ask, expectDone, shot } from "./helpers";

test.use({ viewport: { width: 390, height: 780 } });

test("on a phone the menu slides over and the chat and cards fit the screen", async ({ page }) => {
  await page.goto("/chat");
  await expect(page.getByRole("navigation", { name: "Navigation" })).toBeHidden();
  await shot(page, "40-mobile-empty");
  await page.getByRole("button", { name: "Open the menu" }).click();
  await expect(page.getByRole("link", { name: "Scheduled", exact: true })).toBeVisible();
  await shot(page, "41-mobile-menu");
  await page.getByRole("link", { name: "Connectors", exact: true }).click();                  // going somewhere closes the menu
  await expect(page.getByRole("heading", { name: "Connectors" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Navigation" })).toBeHidden();

  await ask(page, "run a shell command");
  await expect(page.getByRole("region", { name: "Waiting for your decision" })).toBeVisible();
  await shot(page, "42-mobile-approval");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);                                       // nothing pushes the page sideways
  await page.getByRole("region", { name: "Waiting for your decision" }).getByRole("button", { name: "Approve" }).click();
  await expectDone(page);
});
