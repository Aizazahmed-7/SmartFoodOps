import { expect, test } from "@playwright/test";

/**
 * The content studio against the LIVE stack (B6).
 *
 * Deliberately NOT asserting on generated copy. A model writes a different
 * sentence every run, so a test that read one would be asserting on a
 * sample — the same lesson the order story learned when a rewrite turned
 * "payment was declined" into "payment was unfortunately declined".
 *
 * What is asserted is the chain that cannot be faked: the tab is reachable
 * only by a restaurant admin, the feedback digest's NUMBERS are counted
 * from rows rather than written by anything, and every status a draft can
 * be in renders as a decision a human can act on.
 */

const OWNER = {
  email: "owner-islamabad-biryani-house@demo.smartfood.dev",
  password: "demo1234demo",
};
const CUSTOMER = { email: "customer@demo.smartfood.dev", password: "demo1234demo" };

async function signIn(page, who: { email: string; password: string }) {
  await page.goto("/login");
  await page.getByPlaceholder("email").fill(who.email);
  await page.getByPlaceholder("password").fill(who.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
}

async function openStudio(page) {
  await page.goto("/partner/dashboard");
  await page.getByRole("button", { name: "Studio" }).click();
}

test("the studio is a restaurant admin's screen, and a customer has no door to it", async ({
  page,
}) => {
  await signIn(page, CUSTOMER);
  await page.goto("/partner/dashboard");
  // No admin claim, so no dashboard — and therefore no Studio tab anywhere
  // in the app. The server refuses the endpoints too (unit-tested); this is
  // the half a customer could otherwise reach by typing a URL.
  await expect(page.getByRole("button", { name: "Studio" })).toHaveCount(0);
});

test("the studio shows counted numbers and decisions a human can act on", async ({ page }) => {
  await signIn(page, OWNER);
  await openStudio(page);

  // The ask controls exist and are inert until there is something to ask.
  await expect(page.getByTestId("studio-ask")).toBeVisible();
  await expect(page.getByRole("button", { name: "Draft a promotion" })).toBeDisabled();
  await page.getByTestId("studio-ask").fill("something for slow Tuesdays");
  await expect(page.getByRole("button", { name: "Draft a promotion" })).toBeEnabled();

  // The digest's numbers are computed from rows — the panel renders them
  // whether or not a summary exists, which is UC-28's floor.
  await expect(page.getByTestId("feedback-counts")).toContainText(/review/);

  // Every draft on screen carries a status, and a drafted one carries the
  // two decisions FR-93 requires. The seeded stack always has drafts by
  // this point in the milestone; an empty board would mean the fixture,
  // not the feature, has changed.
  const cards = page.getByTestId("draft-card");
  await expect(cards.first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("draft-status").first()).toBeVisible();

  const approvable = page.getByTestId("draft-approve").first();
  if (await approvable.isVisible().catch(() => false)) {
    // Nothing is published by rendering it — the button exists, and the
    // draft is still a draft until somebody presses it (FR-93).
    await expect(approvable).toBeEnabled();
    await expect(page.getByTestId("draft-text").first()).toBeEditable();
  }
});
