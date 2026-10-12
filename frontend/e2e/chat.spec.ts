import { expect, test } from "@playwright/test";
import { ask, expectDone, shot } from "./helpers";

test("the empty chat greets you and offers examples", async ({ page }) => {
  await page.goto("/chat");
  await expect(page.getByRole("heading", { name: /What should we work on, dev\?/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Research something/ })).toBeVisible();
  await shot(page, "01-empty");
});

test("a question is answered with streamed markdown and the conversation appears in the sidebar", async ({ page }) => {
  const text = `hello there ${Date.now()}`;   // the dev stack keeps every conversation, so each run uses its own words
  await ask(page, text);
  await expect(page.getByText(text).first()).toBeVisible();
  await expect(page.getByText("Here is a short overview")).toBeVisible();
  await expectDone(page);
  await expect(page.getByRole("heading", { name: "What should we work on" })).toHaveCount(0);
  await expect(page.locator(".md pre code")).toContainText("def greet");
  await expect(page.getByLabel("Conversations").getByText(text)).toBeVisible();
  await shot(page, "02-answer");
  await page.reload();                       // the conversation survives a reload
  await expect(page.getByText("Here is a short overview")).toBeVisible();
});
