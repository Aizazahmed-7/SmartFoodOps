import { expect, Page, test } from "@playwright/test";

/** The seeded cast (tools/seed): a customer with a saved address, and the
 * owner of Biryani House. Fixture credentials, checked into the repo. */
const CUSTOMER = { email: "customer@demo.smartfood.dev", password: "demo1234demo" };
const OWNER = {
  email: "owner-springfield-biryani-house@demo.smartfood.dev",
  password: "demo1234demo",
};

async function signIn(page: Page, who: { email: string; password: string }) {
  await page.goto("/login");
  await page.getByPlaceholder("email").fill(who.email);
  await page.getByPlaceholder("password").fill(who.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
}

/** Browse → Biryani House → add the first item → cart → checkout.
 *
 * The Add button may or may not open an options modal depending on the
 * item's modifier groups — the spec handles BOTH by trying the modal's
 * "Add to cart" and shrugging if none appeared (a lesson bought during
 * this suite's own development: asserting which items have modals couples
 * the test to seed data it does not own). The one truth asserted is the
 * cart's SERVER quote line — if nothing landed in the cart, that line
 * cannot exist. */
async function fillCart(page: Page) {
  await page.goto("/");
  await page.getByText("Biryani House").click();
  await expect(page.getByRole("heading", { name: "Mutton Karahi" })).toBeVisible();
  // Retry the whole add-gesture until the STORE proves it landed: the
  // click may race hydration, and the item may or may not open an options
  // modal — localStorage ("sfo-cart", zustand persist) is the one signal
  // independent of both.
  // Every click is TIME-BOXED: Playwright's default action timeout is
  // unlimited, so an un-actionable element would silently consume the
  // whole test budget and the retry loop would never get its retry.
  for (let attempt = 0; ; attempt++) {
    const modalAdd = page.getByRole("button", { name: /Add to cart/ });
    // Order matters: clicking "Add" while a modal is already up would hit
    // the backdrop and CLOSE it — the loop would kill its own progress
    // (it did, during this suite's development). So: only click Add when
    // no modal is showing, then give the modal-click a real wait — it
    // doubles as the "did a modal appear at all" probe.
    if (!(await modalAdd.isVisible().catch(() => false))) {
      await page
        .getByRole("button", { name: "Add" })
        .first()
        .click({ timeout: 3_000 })
        .catch(() => {});
    }
    if (await modalAdd.isVisible().catch(() => false)) {
      // "Add to cart" is disabled until required groups are satisfied
      // (min_select) — pick the chip rendered just before it (the last
      // option of the last group) rather than hardcoding seed option
      // names. THE bug this loop kept hitting: a disabled button passes
      // every visibility check and fails every click, silently.
      await modalAdd
        .locator("xpath=preceding::button[1]")
        .click({ timeout: 1_500 })
        .catch(() => {});
    }
    await modalAdd.click({ timeout: 2_500 }).catch(() => {});
    const landed = await page.evaluate(
      () => (localStorage.getItem("sfo-cart") ?? "").includes('"qty"'),
    );
    if (landed) break;
    if (attempt >= 7) throw new Error("nothing ever landed in the cart");
    await page.waitForTimeout(700);
  }
  await page.goto("/cart");
  await expect(page.getByText("Priced by the server")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: /Checkout/ }).click();
  await expect(page.getByText("Pay with")).toBeVisible({ timeout: 10_000 });
}

async function placeOrder(page: Page): Promise<string> {
  await page.getByRole("button", { name: /Place order/ }).click();
  // Success navigates to /orders/{id} — the id is the URL's tail.
  await page.waitForURL(/\/orders\/ord_[a-f0-9]+/);
  const orderId = page.url().split("/").pop()!;
  return orderId;
}

test("the two-window story: place, kitchen drives, courier delivers, settles", async ({
  page: cPage,
  browser,
}) => {
  // Window 1 (the config-aware default fixture): the customer.
  await signIn(cPage, CUSTOMER);
  await fillCart(cPage);
  await cPage.getByText("Visa ····4242 (approves)").click();
  const orderId = await placeOrder(cPage);

  // The saga reserves stock and clears payment without any human.
  await expect(cPage.getByText("CONFIRMED")).toBeVisible({ timeout: 30_000 });

  // Window 2: the owner's kitchen feed. browser.newContext() inherits
  // NOTHING from the config's `use` block — baseURL must be restated or
  // every goto("/…") in this context misbehaves (a lesson this suite paid
  // for in debugging time).
  const partner = await browser.newContext({ baseURL: "http://localhost:5173" });
  const pPage = await partner.newPage();
  await signIn(pPage, OWNER);
  await pPage.goto("/partner/dashboard");
  const card = pPage.locator(".card", { hasText: orderId.slice(0, 12) });
  await expect(async () => {
    await card.getByRole("button", { name: "Accept" }).click();
    // The card moves queues once the accept lands — its next action proves it.
    await expect(card.getByRole("button", { name: "Start preparing" })).toBeVisible({
      timeout: 4_000,
    });
  }).toPass({ timeout: 30_000 });
  await card.getByRole("button", { name: "Start preparing" }).click();
  await card.getByRole("button", { name: "Food is ready" }).click({ timeout: 20_000 });

  // Window 1 again: the simulated courier (20s + 30s timers) finishes the
  // job; capture and settlement follow with no further clicks anywhere.
  await expect(cPage.getByText(/DELIVERED|SETTLED/)).toBeVisible({ timeout: 90_000 });

  await partner.close();
});

test("the declined card becomes order state, never an error page", async ({ page }) => {
  await signIn(page, CUSTOMER);
  await fillCart(page);
  await page.getByText("Visa ····0002 (declines)").click();
  await placeOrder(page);

  // The 402 lives inside the saga: the order card reports the decline as a
  // lifecycle outcome with honest copy — not a toast, not a 500.
  // exact: the tag reads "CANCELLED"; the banner CONTAINS "cancelled" too,
  // and matching both is a strict-mode violation once both have rendered.
  await expect(page.getByText("CANCELLED", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("your card was declined")).toBeVisible();
});
