import { expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

export const shot = async (page: Page, name: string) => {
  mkdirSync("screenshots", { recursive: true });
  await page.screenshot({ path: `screenshots/${name}.png` });
};

/** Start a fresh conversation and send one message. */
export async function ask(page: Page, text: string) {
  await page.goto("/chat");
  await page.getByLabel("Message").fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
}

export async function expectDone(page: Page) {
  await expect(page.getByText("Working…")).toHaveCount(0, { timeout: 20_000 });
}
