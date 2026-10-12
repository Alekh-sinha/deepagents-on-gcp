import { expect, test } from "@playwright/test";
import { ask, expectDone, shot } from "./helpers";

test("a command waits for approval as a card, runs when approved, and is kept as a row", async ({ page }) => {
  await ask(page, "run a shell command");
  const card = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(card).toBeVisible();
  await expect(card.getByText("Run a shell command")).toBeVisible();
  await expect(card.getByText("echo hello from the sandbox && ls")).toBeVisible();
  await expect(page.getByLabel("Message")).toBeDisabled();                       // the next message waits for this answer
  await expect(page.getByLabel("Message")).toHaveAttribute("placeholder", "Answer the request above to continue");
  await shot(page, "03-approval-shell");
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("The command ran. Output: ")).toBeVisible();
  await expectDone(page);
  await expect(page.getByRole("button", { name: /Approved/ })).toBeVisible();
  await expect(page.getByLabel("Message")).toBeEnabled();
  await shot(page, "04-approval-done");
});

test("rejecting tells the assistant and the turn carries on", async ({ page }) => {
  await ask(page, "run a shell command");
  const card = page.getByRole("region", { name: "Waiting for your decision" });
  await card.getByRole("button", { name: "Reject" }).click();
  await card.getByLabel("Why not? (optional)").fill("not now");
  await card.getByRole("button", { name: "Reject" }).click();
  await expectDone(page);
  await expect(page.getByRole("button", { name: /Rejected/ })).toBeVisible();
});

test("a plan can be edited before it is approved", async ({ page }) => {
  await ask(page, "make a plan");
  const card = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(card.getByText("Write a short report and save it")).toBeVisible();
  await expect(card.getByText("Collect the key points")).toBeVisible();
  await shot(page, "05-approval-plan");
  await card.getByRole("button", { name: "Edit" }).click();
  await card.getByRole("button", { name: "Remove step 2" }).click();
  await shot(page, "06-approval-plan-edit");
  await card.getByRole("button", { name: "Approve with my changes" }).click();
  await expect(page.getByText("Plan approved. Working through it now.")).toBeVisible();
});

test("a question is answered in the card, with suggested answers", async ({ page }) => {
  await ask(page, "ask me something");
  const card = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(card.getByText("Which format do you want the report in?")).toBeVisible();
  await expect(card.getByRole("button", { name: /^Approve/ })).toHaveCount(0);     // a question cannot be "approved"
  await card.getByRole("button", { name: "PDF" }).click();
  await card.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Thanks, I'll use that: PDF")).toBeVisible();
});

test("two helpers pausing together are one group, answered with one click", async ({ page }) => {
  await ask(page, "do it in parallel");
  const card = page.getByRole("region", { name: "Waiting for your decision" });
  await expect(card.getByText("2 things are waiting for your decision")).toBeVisible();
  await shot(page, "07-approval-batch");
  await card.getByRole("button", { name: "Approve all 2" }).click();
  await expect(page.getByText("Both helpers are done.")).toBeVisible();
  await expectDone(page);
  await expect(page.getByText("gemini-2.5-flash-lite").first()).toBeVisible();     // each helper shows the model chosen for it
  await shot(page, "08-helpers");
});

test("automatic approval, once on, stays on and is marked in the conversation", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: /Ask before acting/ }).click();
  await shot(page, "09-auto-menu");
  await page.getByRole("switch", { name: "Approve automatically" }).click();
  await expect(page.getByRole("button", { name: /Auto-approving/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("Message").fill("run a shell command");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText("The command ran. Output: ")).toBeVisible();
  await expect(page.getByRole("region", { name: "Waiting for your decision" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Approved automatically/ })).toBeVisible();
  await expectDone(page);
  await shot(page, "10-auto-approved");
  await page.getByLabel("Message").fill("run a shell command again");   // the setting stays for the conversation
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("button", { name: /Approved automatically/ })).toHaveCount(2);
  await page.reload();                                                 // ...and survives a reload
  await expect(page.getByRole("button", { name: /Auto-approving/ })).toBeVisible();
  await page.getByRole("button", { name: /Auto-approving/ }).click();
  await page.getByRole("switch", { name: "Approve automatically" }).click();
  await expect(page.getByRole("button", { name: /Ask before acting/ })).toBeVisible();
});
