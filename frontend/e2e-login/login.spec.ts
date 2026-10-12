import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

/** A stand-in for Google's script: it draws a button that, when pressed, gives the page the credential the test chose. */
async function stubGoogle(page: Page, credential: string) {
  await page.route("https://accounts.google.com/gsi/client", (route) => route.fulfill({
    contentType: "text/javascript",
    body: `window.google = { accounts: { id: {
      initialize(o) { window.__cb = o.callback; },
      renderButton(el) { const b = document.createElement("button"); b.textContent = "Sign in with Google"; b.onclick = () => window.__cb({ credential: ${JSON.stringify(credential)} }); el.appendChild(b); }
    } } };`,
  }));
}

test("signing in with Google leads to the app, survives a reload, and signing out returns to the sign-in page", async ({ page }) => {
  await stubGoogle(page, "fake-google-credential-ok");
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  mkdirSync("screenshots", { recursive: true });
  await page.screenshot({ path: "screenshots/30-login.png" });
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(page.getByRole("heading", { name: /What should we work on/ })).toBeVisible();
  await page.reload();                                                        // the session cookie keeps you in
  await expect(page.getByRole("heading", { name: /What should we work on/ })).toBeVisible();
  const cookies = await page.context().cookies();
  const session = cookies.find((c) => c.name === "privai_session")!;
  expect(session.httpOnly).toBe(true);                                        // scripts on the page cannot read it
  expect(session.sameSite).toBe("Lax");
  await page.getByRole("button", { name: /dev@example.com/ }).click();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();   // really signed out, not just hidden
});

test("a Google account that is not on the allow-list is turned away with a reason", async ({ page }) => {
  await stubGoogle(page, "fake-google-credential-stranger");
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(page.getByRole("alert")).toContainText("not allowed to use privai");
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  expect((await page.context().cookies()).find((c) => c.name === "privai_session")).toBeUndefined();
  await page.screenshot({ path: "screenshots/31-login-refused.png" });
});

test("a credential Google would not vouch for is refused", async ({ page }) => {
  await stubGoogle(page, "forged-credential-0123456789");
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(page.getByRole("alert")).toContainText("could not verify");
});

test("reaching a page of the app while signed out shows the sign-in page", async ({ page }) => {
  await stubGoogle(page, "fake-google-credential-ok");
  await page.goto("/schedules");
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
});
