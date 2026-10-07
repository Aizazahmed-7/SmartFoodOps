import { expect, Page, test } from "@playwright/test";

/**
 * The assistant panel against the LIVE stack (make up-ai), same philosophy
 * as the order story: a real LangGraph turn, real retrieval and a real
 * provider sit behind every click. What is asserted is the chain that
 * cannot be faked — a streamed answer, and a CARD under it whose price came
 * from catalog at read time rather than from the answer's prose (FR-60).
 *
 * Islamabad, not the default city: it is the city the knowledge index has
 * chunks for, and an assistant with nothing to retrieve answers NO_MATCH
 * correctly and proves nothing.
 */

/** This spec registers its OWN customer rather than borrowing the seeded
 * one. Asking a question needs nothing but an identity — no address, no
 * kitchen, no saga — so depending on `make seed` would couple a panel test
 * to a world it does not use, and the assistant's index is built from the
 * hand-built cities anyway. Registration is idempotent here in practice:
 * a second run signs in instead. */
const CUSTOMER = { email: "assistant-e2e@demo.smartfood.dev", password: "demo1234demo" };

async function signIn(page: Page) {
  await page.goto("/register");
  await page.getByPlaceholder("email").fill(CUSTOMER.email);
  await page.getByPlaceholder(/password/).fill(CUSTOMER.password);
  await page.getByRole("button", { name: /Create account|Register|Sign up/ }).click();
  // Already registered from an earlier run — sign in with the same details.
  if (!(await page.getByRole("button", { name: "Sign out" }).isVisible().catch(() => false))) {
    await page.goto("/login");
    await page.getByPlaceholder("email").fill(CUSTOMER.email);
    await page.getByPlaceholder(/password/).fill(CUSTOMER.password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
}

async function openPanel(page: Page) {
  await page.goto("/");
  // The panel is geo-scoped from the shared city store, so picking the city
  // on Browse is what points the question at a stocked index.
  await page.getByRole("button", { name: "Islamabad" }).click();
  await page.getByTestId("assistant-open").click();
  await expect(page.getByTestId("assistant-panel")).toBeVisible();
}

test("a question streams a grounded answer with live-priced cards", async ({ page }) => {
  await signIn(page);
  await openPanel(page);

  await page.getByTestId("assistant-input").fill("something light and not too spicy");
  await page.getByRole("button", { name: "Ask", exact: true }).last().click();

  const panel = page.getByTestId("assistant-panel");
  // The question appears immediately — the POST is 202 and the answer does
  // not exist yet.
  await expect(panel.getByText("something light and not too spicy")).toBeVisible();

  // Tokens arrive over SSE. A generation takes seconds, so this is the one
  // wait the spec is generous with.
  //
  // A CARD, not an Add button: a dish with a required modifier group shows
  // "Choose options" instead (adding it blind builds a cart line the quote
  // endpoint refuses), and which dishes the model cites is not something
  // this test should depend on.
  await expect(panel.getByTestId("assistant-card").first()).toBeVisible({ timeout: 60_000 });

  // A card means the whole chain held: the answer cited a marker, grounding
  // kept it because it was retrieved, the id survived onto the message row,
  // and catalog priced it live. None of that can be faked by a model.
  //
  // Matched as a whole price rather than a bare "$": the budget chips read
  // "Under $5", so a substring match finds the controls instead of the
  // thing being asserted.
  await expect(panel.locator("text=/^\\$[0-9]+\\.[0-9]{2}$/").first()).toBeVisible();
});

test("a dish from the answer lands in the cart", async ({ page }) => {
  await signIn(page);
  await openPanel(page);
  const panel = page.getByTestId("assistant-panel");

  await page.getByTestId("assistant-input").fill("what do you recommend?");
  await page.getByRole("button", { name: "Ask", exact: true }).last().click();

  // Wait for the answer's cards, then take one that is actually addable —
  // a dish needing options is a different (correct) path.
  await expect(panel.getByTestId("assistant-card").first()).toBeVisible({ timeout: 60_000 });
  const add = panel.getByTestId("assistant-add").first();
  await expect(add).toBeVisible({ timeout: 20_000 });
  await add.click();

  // The store is the one signal independent of rendering — the same proof
  // the order story uses for its own add-to-cart gesture.
  await expect
    .poll(
      async () =>
        await page.evaluate(() => {
          const raw = localStorage.getItem("sfo-cart");
          return raw ? JSON.parse(raw)?.state?.lines?.length ?? 0 : 0;
        }),
      { timeout: 10_000 },
    )
    .toBeGreaterThan(0);
});

test("the panel suggests dishes before anything is typed, and honours a budget", async ({
  page,
}) => {
  await signIn(page);
  await openPanel(page);
  const panel = page.getByTestId("assistant-panel");

  // FR-80: never an empty response for a customer who has typed nothing.
  await expect(panel.getByText(/Based on what you usually order|Popular near you/)).toBeVisible({
    timeout: 20_000,
  });
  // `assistant-card`, not `assistant-add` — the same lesson the two tests
  // above already learned. A dish with a required modifier group renders
  // "Choose options" instead of an Add button (`needs_choice`), so
  // asserting on Add couples this test to whichever dishes happen to be
  // popular in the window. What FR-80 promises is a SUGGESTION, and the
  // card is the suggestion.
  await expect(panel.getByTestId("assistant-card").first()).toBeVisible({ timeout: 20_000 });

  // FR-76: a budget is a hard predicate. Every price still on screen must
  // be under it — asserted on the RENDERED text, because that is the promise
  // the customer actually reads.
  await panel.getByTestId("assistant-budget-500").click();
  await expect(panel.getByText("Under $5")).toBeVisible();
  await page.waitForTimeout(2_000);
  const prices = await panel.getByTestId("assistant-add").count();
  if (prices > 0) {
    const shown = await panel.locator("text=/^\\$[0-9]+\\.[0-9]{2}$/").allInnerTexts();
    for (const price of shown) {
      expect(Number(price.replace("$", ""))).toBeLessThanOrEqual(5);
    }
  }
});
