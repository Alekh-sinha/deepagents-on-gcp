import { expect, test } from "@playwright/test";
import { ask, expectDone, shot } from "./helpers";

test("a progress list shows what the assistant is working through", async ({ page }) => {
  await ask(page, "make a todo list");
  const panel = page.getByRole("button", { name: /Progress/ });
  await expect(panel).toBeVisible();
  await expect(page.getByText("1 of 3 done")).toBeVisible();
  await expect(page.getByText("Draft the summary")).toBeVisible();
  await expectDone(page);
  await shot(page, "11-todos");
  await panel.click();                                                  // collapses
  await expect(page.getByText("Draft the summary")).toHaveCount(0);
});

test("a helper on a cheaper model is shown as its own block with that model", async ({ page }) => {
  await ask(page, "research something");
  await expect(page.getByText("The researcher found it:")).toBeVisible();
  await expectDone(page);
  const block = page.getByRole("button", { name: /^researcher/i });
  await expect(block).toBeVisible();
  await expect(block).toContainText("gemini-2.5-flash-lite");
  await expect(page.getByText("(from the helper, on gemini-2.5-flash-lite)").first()).toBeVisible();
  await shot(page, "12-helper");
});

test("a saved file appears as a download, and in the Files page", async ({ page }) => {
  await ask(page, "write a file for me");
  await expect(page.getByText("I saved the report.")).toBeVisible();
  const file = page.getByRole("link", { name: /report\.md/ }).first();
  await expect(file).toBeVisible();
  await shot(page, "13-file");
  const res = await page.request.get((await file.getAttribute("href"))!);
  expect(res.status()).toBe(200);
  expect(await res.text()).toContain("This file was written by the demo model.");
  await page.getByRole("link", { name: "Files" }).click();
  await expect(page.getByRole("heading", { name: "Files" })).toBeVisible();
  await expect(page.getByText("report.md").first()).toBeVisible();
  await shot(page, "14-files-page");
});

test("a long answer can be stopped", async ({ page }) => {
  await ask(page, "give me a slow answer");
  await expect(page.getByText("Sentence number 1 of").first()).toBeVisible();
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.getByText("You stopped this.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);
  await expect(page.getByLabel("Message")).toBeEnabled();
});

test("an error is shown plainly and the conversation stays usable", async ({ page }) => {
  await ask(page, "please fail");
  await expect(page.getByRole("status").filter({ hasText: "the demo model was asked to fail" })).toBeVisible();
  await shot(page, "15-error");
  await page.getByLabel("Message").fill("hello again");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText("Here is a short overview")).toBeVisible();
});

test("the model can be chosen, and the choice is remembered for the next chat", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: /Gemini 2.5 Flash/ }).click();
  await expect(page.getByRole("option")).toHaveCount(10);
  await shot(page, "16-model-picker");
  await page.getByRole("option", { name: /Gemini 3.5 Flash-Lite/ }).click();
  await expect(page.getByRole("button", { name: /Gemini 3.5 Flash-Lite/ })).toBeVisible();
  await page.goto("/chat");
  await expect(page.getByRole("button", { name: /Gemini 3.5 Flash-Lite/ })).toBeVisible();
  await page.getByRole("button", { name: /Gemini 3.5 Flash-Lite/ }).click();
  await page.getByRole("option", { name: /^Gemini 2\.5 Flash rank/ }).click();   // put it back
});

test("an example on the empty chat fills the message box", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: /Write a document/ }).click();
  await expect(page.getByLabel("Message")).toHaveValue(/one-page brief/);
});

test("archiving a conversation removes it from the list", async ({ page }) => {
  const text = `to archive ${Date.now()}`;
  await ask(page, text);
  await expect(page.getByLabel("Conversations").getByText(text)).toBeVisible();
  await page.getByRole("button", { name: /Archive/ }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByLabel("Conversations").getByText(text)).toHaveCount(0);
});
