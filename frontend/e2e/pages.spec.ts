import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { ask, expectDone, shot } from "./helpers";

const mcpUrl = (): string => JSON.parse(readFileSync(".dev-stack.json", "utf8")).mcp_url;
const config = (url: string, name = "calc") => JSON.stringify({ mcpServers: { [name]: { url, headers: { Authorization: "Bearer s3cret" } } } });

/** The dev stack keeps what earlier runs left behind (re-importing a server rightly keeps the user's "safe" marks), so start from nothing. */
const forget = async (page: import("@playwright/test").Page, name: string) => { await page.request.delete(`/api/mcp/servers/${name}`, { headers: { "X-Requested-With": "pinboard" } }); };

test.describe("connectors", () => {
  test("a pasted server connects, shows its tools, and is used with approval until a tool is marked safe", async ({ page }) => {
    await forget(page, "calc");
    await page.goto("/connectors");
    await page.getByLabel("Server configuration").fill(config(mcpUrl()));
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page.getByRole("list", { name: "Import results" })).toContainText("connected, 4 tools");
    const card = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: "calc" }) });
    await expect(card.getByText(/Authorization set/)).toBeVisible();                   // the token's name is shown, never its value
    await expect(page.getByText("s3cret")).toHaveCount(0);
    await shot(page, "17-connectors");
    await card.getByRole("button", { name: "Tools" }).click();
    await expect(card.getByText("Add two whole numbers.")).toBeVisible();
    await shot(page, "18-connector-tools");

    await ask(page, "use the mcp calculator");                                          // asks first
    const approval = page.getByRole("region", { name: "Waiting for your decision" });
    await expect(approval.getByText("Use add on calc")).toBeVisible();
    await shot(page, "19-approval-mcp");
    await approval.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByText(/The connected tool answered: .*42/)).toBeVisible();
    await expectDone(page);

    await page.goto("/connectors");                                                    // mark the tool safe: no more asking
    await card.getByRole("button", { name: "Tools" }).click();
    await card.getByLabel("Ask before add").uncheck();
    await expect(card.getByLabel("Ask before add")).not.toBeChecked();
    await ask(page, "use the mcp calculator again");
    await expect(page.getByText(/The connected tool answered: .*42/)).toBeVisible();
    await expect(page.getByRole("region", { name: "Waiting for your decision" })).toHaveCount(0);

    await page.goto("/connectors");                                                    // switch it off, then remove it
    await page.getByRole("switch", { name: "Use calc" }).click();
    await expect(page.getByText("off", { exact: true })).toBeVisible();
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Remove" }).click();
    await expect(page.getByRole("heading", { name: "calc" })).toHaveCount(0);
  });

  test("a config that cannot work is refused with the reason", async ({ page }) => {
    await page.goto("/connectors");
    await page.getByLabel("Server configuration").fill("{not json");
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page.getByRole("alert")).toContainText("not valid JSON");
    await page.getByLabel("Server configuration").fill(JSON.stringify({ mcpServers: { local: { command: "npx", args: ["something"] } } }));
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page.getByRole("alert")).toContainText("stdio");
  });

  test("a server that is not reachable is saved with its error so it can be fixed", async ({ page }) => {
    await page.goto("/connectors");
    await page.getByLabel("Server configuration").fill(JSON.stringify({ mcpServers: { broken: { url: "http://127.0.0.1:9/mcp" } } }));
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page.getByRole("list", { name: "Import results" })).toContainText("saved, but not reachable yet");
    const card = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: "broken" }) });
    await expect(card.getByLabel("not reachable")).toBeVisible();
    await shot(page, "20-connector-broken");
    page.once("dialog", (d) => d.accept());
    await card.getByRole("button", { name: "Remove" }).click();
  });
});

test.describe("scheduled tasks", () => {
  test("create, run now, read the history, open the run, pause, edit and delete", async ({ page }) => {
    const title = `Morning brief ${Date.now()}`;
    await page.goto("/schedules");
    await page.getByRole("button", { name: "New task" }).click();
    await page.getByLabel("Name").fill(title);
    await page.getByLabel("What should it do?").fill("Say hello and summarise what you can do.");
    await shot(page, "21-schedule-form");
    await page.getByRole("button", { name: "Create" }).click();
    const card = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: title }) });
    await expect(card.getByText(/Weekdays at 9:00/)).toBeVisible();
    await expect(card.getByText(/Next run/)).toBeVisible();
    await shot(page, "22-schedules");

    await card.getByRole("button", { name: "Run now" }).click();
    await expect(card.getByText(/Ran .*1 run/)).toBeVisible();
    await card.getByRole("button", { name: "History" }).click();
    await expect(card.getByText("by hand")).toBeVisible();
    await shot(page, "23-schedule-history");
    await card.getByRole("link", { name: "Open" }).click();                              // the run is its own conversation
    await expect(page.getByText("Scheduled", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Say hello and summarise what you can do.").first()).toBeVisible();
    await expect(page.getByText("Here is a short overview")).toBeVisible();
    await shot(page, "24-scheduled-run");

    await page.goto("/schedules");
    await card.getByRole("switch", { name: `Enable ${title}` }).click();
    await expect(card.getByText("paused")).toBeVisible();
    await card.getByRole("button", { name: "Edit" }).click();
    await page.getByLabel("Name").fill(`${title} (edited)`);
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("heading", { name: `${title} (edited)` })).toBeVisible();
    page.once("dialog", (d) => d.accept());
    await page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: `${title} (edited)` }) }).getByRole("button", { name: "Delete" }).click();
    await expect(page.getByRole("heading", { name: `${title} (edited)` })).toHaveCount(0);
  });

  test("a schedule that fires too often is refused with the reason", async ({ page }) => {
    await page.goto("/schedules");
    await page.getByRole("button", { name: "New task" }).click();
    await page.getByLabel("Name").fill("Too often");
    await page.getByLabel("What should it do?").fill("Anything.");
    await page.getByLabel("How often").selectOption("custom");
    await page.getByLabel("Cron expression").fill("*/5 * * * *");
    await page.getByRole("button", { name: "Create" }).click();
    await expect(page.getByRole("alert")).toContainText("minimum interval");
    await expect(page.getByRole("heading", { name: "Too often" })).toHaveCount(0);
  });
});

test("settings show the account and what the deployment allows", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByText("dev@example.com").first()).toBeVisible();
  await expect(page.getByText("Gemini 3.1 Pro (preview)")).toBeVisible();
  await expect(page.getByText(/only to dev@example.com/)).toBeVisible();
  await shot(page, "25-settings");
});

test("the sidebar offers a way to sign out and to open settings", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: /dev@example.com/ }).click();
  await expect(page.getByRole("link", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
});
