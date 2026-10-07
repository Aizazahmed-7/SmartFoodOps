import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  askAssistant,
  getAssistantItems,
  getAssistantTicket,
  getRecommendations,
} from "../api/client";
import type {
  AssistantCard,
  AssistantChunk,
  AssistantCombo,
  Recommendations,
} from "../api/types";
import { useAuth } from "../state/auth";
import { useCart } from "../state/cart";
import { useCity } from "../state/city";
import { Money } from "./ui";

/** One exchange on screen. `cards` arrive after the answer finishes — they
 * are priced at read time, never carried in the prose (FR-60). */
interface Turn {
  id: string;
  question: string;
  answer: string;
  cards: AssistantCard[];
  /** Highest `seq` seen. This is the resume cursor, and it is per TURN
   * because that is the scope the server sequences (ADR-0042 §7). */
  seq: number;
  done: boolean;
  failed?: boolean;
  /** The answer arrived; pricing its dishes did not. Kept apart from
   * `failed` — the prose is fine and the customer should still read it. */
  cardsFailed?: boolean;
}

/** How many times a single answer may re-ticket before we stop.
 *
 * Bounded because the failure this recovers from — a dropped socket — is
 * indistinguishable from one the server will never accept, and an
 * unbounded retry against the second case is a browser tab that buys
 * tickets forever. */
const MAX_RESUMES = 5;

export default function AssistantPanel() {
  const { claims } = useAuth();
  const city = useCity((s) => s.city);
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  // What to order before anyone has typed anything (FR-80). Fetched when
  // the panel opens rather than on mount: reading it RECORDS a showing
  // (FR-79), and a panel nobody opened showed nothing.
  const [suggestions, setSuggestions] = useState<Recommendations | null>(null);
  const [budget, setBudget] = useState<number | undefined>(undefined);
  const conversation = useRef<string | undefined>(undefined);
  const esRef = useRef<EventSource | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);

  useEffect(() => () => esRef.current?.close(), []);

  // The panel never unmounts — `if (!claims) return null` below is a render
  // bail, not an unmount — so nothing here was ever cleared on sign-out.
  // The next customer on a shared browser saw the previous one's questions,
  // answer and cards, and their first question inherited the old
  // `conversation_id`. A stream open on the old customer's ticket also kept
  // writing into the panel (B3 review). Keyed on `sub` rather than truthiness
  // so an account SWITCH resets too, not just a sign-out.
  const sub = claims?.sub ?? null;
  useEffect(() => {
    esRef.current?.close();
    esRef.current = null;
    conversation.current = undefined;
    setTurns([]);
    setSuggestions(null);
    setBudget(undefined);
    // `open` too: `if (!claims) return null` is a render bail, not an
    // unmount, so it survived a sign-out — and the next customer on a
    // shared browser got a panel already open, plus a recorded showing
    // they never asked for (B4 review).
    setOpen(false);
    setBusy(false);
    setQuestion("");
  }, [sub]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, open]);

  useEffect(() => {
    // `sub`, not `claims`: the store hands back a fresh claims OBJECT on
    // every token refresh, so a background refresh re-ran this and wrote a
    // second showing for one panel open. And `turns.length` gates it
    // because reading recommendations RECORDS a showing — fetching for a
    // list the panel is not rendering puts a row in the denominator for a
    // surface no pixel of which exists (B4 review).
    if (!open || !sub || turns.length > 0) return;
    let stale = false;
    // Clear first: the header switches city immediately, so leaving the
    // previous city's dishes on screen with live Add buttons let a customer
    // put an out-of-city restaurant in the cart during the round trip.
    setSuggestions(null);
    getRecommendations(city, budget)
      .then((found) => {
        if (!stale) setSuggestions(found);
      })
      // An empty panel is a worse panel, not a broken one — the customer
      // can still type.
      .catch(() => {
        if (!stale) setSuggestions(null);
      });
    return () => {
      stale = true;
    };
  }, [open, sub, city, budget, turns.length]);

  const patch = (id: string, change: Partial<Turn>) =>
    setTurns((all) => all.map((t) => (t.id === id ? { ...t, ...change } : t)));

  /** Follow one answer, re-ticketing across drops.
   *
   * `EventSource` reconnects on its own, but it reuses the URL and
   * therefore the single-use ticket it already spent — so every automatic
   * retry 401s. We close it, buy a fresh ticket and reopen from `seq`,
   * which is the same contract the tracking and bell streams use.
   */
  const follow = (messageId: string, ticket: string, from: number, attempt = 0) => {
    const url = `/sse/assistant/${messageId}?ticket=${encodeURIComponent(ticket)}${
      from > 0 ? `&after=${from}` : ""
    }`;
    // Exactly one live stream per panel. `esRef` only ever held one, so a
    // second question started while the first was still streaming orphaned
    // the earlier EventSource — still open, still appending into its turn.
    esRef.current?.close();
    const es = new EventSource(url);
    esRef.current = es;
    let seen = from;

    es.addEventListener("chunk", (event) => {
      const frame = JSON.parse((event as MessageEvent).data) as AssistantChunk;
      seen = Math.max(seen, frame.seq);
      if (frame.done) {
        es.close();
        patch(messageId, { seq: seen, done: true });
        setBusy(false);
        // The ids are on the terminal frame, but the PRICES are not — so
        // the cards are a second read, against today's menu.
        // Surfaced, not swallowed. A silent `catch` left the answer naming
        // three dishes with nothing to tap, no error, and the panel's own
        // state claiming success — which is what made the conversation-
        // hijack bug invisible in production rather than loud (B3 review).
        getAssistantItems(messageId)
          .then(({ items }) => patch(messageId, { cards: items }))
          .catch(() => patch(messageId, { cardsFailed: true }));
        return;
      }
      setTurns((all) =>
        all.map((t) =>
          t.id === messageId ? { ...t, answer: t.answer + frame.text, seq: seen } : t,
        ),
      );
    });

    const resume = () => {
      es.close();
      if (attempt >= MAX_RESUMES) {
        patch(messageId, { done: true, failed: true });
        setBusy(false);
        return;
      }
      getAssistantTicket(messageId)
        .then((fresh) => follow(messageId, fresh.ticket, seen, attempt + 1))
        .catch(() => {
          patch(messageId, { done: true, failed: true });
          setBusy(false);
        });
    };

    // The server ends a long-lived stream with `reconnect` rather than an
    // EOF, so a deliberate close and a broken socket land in the same place.
    es.addEventListener("reconnect", resume);
    es.onerror = resume;
  };

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    const asked = question.trim();
    if (!asked || busy) return;
    setQuestion("");
    setBusy(true);
    try {
      const started = await askAssistant({
        question: asked,
        city,
        conversation_id: conversation.current,
      });
      conversation.current = started.conversation_id;
      setTurns((all) => [
        ...all,
        { id: started.message_id, question: asked, answer: "", cards: [], seq: 0, done: false },
      ]);
      follow(started.message_id, started.ticket, 0);
    } catch {
      setTurns((all) => [
        ...all,
        {
          id: `local_${Date.now()}`,
          question: asked,
          answer: "",
          cards: [],
          seq: 0,
          done: true,
          failed: true,
        },
      ]);
      setBusy(false);
    }
  };

  if (!claims) return null; // asking requires an identity (FR-67)

  return (
    <>
      <button
        data-testid="assistant-open"
        onClick={() => setOpen((o) => !o)}
        className="fixed bottom-5 right-5 z-20 rounded-full bg-orange-500 px-4 py-3 text-sm font-bold text-slate-950 shadow-lg hover:bg-orange-400"
      >
        {open ? "Close" : "Ask"}
      </button>

      {open && (
        <aside
          data-testid="assistant-panel"
          className="fixed bottom-20 right-5 z-20 flex h-[28rem] w-[22rem] flex-col rounded-xl border border-slate-800 bg-slate-950 shadow-2xl"
        >
          <header className="border-b border-slate-800 px-4 py-2.5">
            <p className="text-sm font-semibold">Ask about the menu</p>
            <p className="text-xs capitalize text-slate-500">{city}</p>
          </header>

          <div className="flex-1 space-y-4 overflow-y-auto px-4 py-3">
            {turns.length === 0 && (
              <div className="space-y-3">
                <p className="text-xs text-slate-500">
                  Try “something light and not too spicy”, or “what goes well with biryani?”
                </p>

                <div className="flex flex-wrap gap-1.5">
                  {/* Presets, not a free-text amount: a budget is a rough
                      intent ("under a fiver"), and asking someone to type
                      cents turns a suggestion into a form. */}
                  {[undefined, 500, 1000, 2000].map((amount) => (
                    <button
                      key={amount ?? "any"}
                      data-testid={`assistant-budget-${amount ?? "any"}`}
                      onClick={() => setBudget(amount)}
                      className={`rounded-full px-2.5 py-1 text-[11px] ${
                        budget === amount
                          ? "bg-orange-500 font-semibold text-slate-950"
                          : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                      }`}
                    >
                      {amount === undefined ? "Any price" : `Under $${amount / 100}`}
                    </button>
                  ))}
                </div>

                {suggestions && suggestions.items.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-xs font-medium text-slate-400">
                      {/* The basis is shown, because "because you order this
                          sort of thing" and "popular near you" are different
                          promises and a customer can tell when one is
                          dressed as the other (FR-75). */}
                      {suggestions.basis === "taste"
                        ? "Based on what you usually order"
                        : "Popular near you right now"}
                    </p>
                    {suggestions.items.map((card) => (
                      <AssistantItem
                        key={card.item_id}
                        card={card}
                        onOpen={() => setOpen(false)}
                      />
                    ))}
                  </div>
                )}

                {suggestions && suggestions.combos.length > 0 && (
                  <div className="space-y-2">
                    <p className="text-xs font-medium text-slate-400">Often ordered together</p>
                    {suggestions.combos.map((combo) => (
                      <ComboCard
                        key={combo.items.map((i) => i.item_id).join("+")}
                        combo={combo}
                        onOpen={() => setOpen(false)}
                      />
                    ))}
                  </div>
                )}

                {suggestions &&
                  suggestions.items.length === 0 &&
                  suggestions.combos.length === 0 && (
                    <p className="text-xs text-slate-500">
                      {/* Gated on BOTH, or the panel says nothing fits the
                          budget directly above a combo that does — items and
                          combos are drawn from different pools server-side. */}
                      {budget
                        ? "Nothing on the menus near you fits that budget."
                        : "Nothing to suggest here yet."}
                    </p>
                  )}
              </div>
            )}
            {turns.map((turn) => (
              <div key={turn.id} className="space-y-2">
                <p className="ml-auto w-fit max-w-[85%] rounded-lg bg-slate-800 px-3 py-1.5 text-sm">
                  {turn.question}
                </p>
                <div className="max-w-[90%] text-sm text-slate-300">
                  {turn.answer}
                  {/* A caret while tokens are still arriving: an empty
                      bubble that just sits there reads as broken. */}
                  {!turn.done && <span className="ml-0.5 animate-pulse">▌</span>}
                  {turn.failed && !turn.answer && (
                    <span className="text-slate-500">
                      Couldn’t answer that just now — please try again.
                    </span>
                  )}
                </div>
                {turn.cards.map((card) => (
                  <AssistantItem key={card.item_id} card={card} onOpen={() => setOpen(false)} />
                ))}
                {turn.cardsFailed && (
                  <p className="text-xs text-slate-500">
                    Couldn’t load prices for these just now.
                  </p>
                )}
              </div>
            ))}
            <div ref={bottom} />
          </div>

          <form onSubmit={send} className="flex gap-2 border-t border-slate-800 p-3">
            <input
              data-testid="assistant-input"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What are you in the mood for?"
              className="input flex-1 text-sm"
              maxLength={2000}
            />
            <button className="btn-primary text-sm" disabled={busy || !question.trim()}>
              {busy ? "…" : "Ask"}
            </button>
          </form>
        </aside>
      )}
    </>
  );
}

/** Two dishes people order together (FR-77).
 *
 * Shows the pair's total from the same live floors the budget was checked
 * against, and adds BOTH or neither — a half-added combo is not the thing
 * that was suggested, and the cart would quietly cost more than the total
 * the customer just read. */
function ComboCard({ combo, onOpen }: { combo: AssistantCombo; onOpen: () => void }) {
  const add = useCart((c) => c.add);
  // Derived from the cart, like AssistantItem: `add` returns
  // "different-restaurant" and changes nothing when the cart holds another
  // kitchen, and the loop used to discard both return values — so "Add
  // both" was a silent no-op with no feedback at all (B4 review).
  const cartRestaurant = useCart((c) => c.restaurantId);
  const navigate = useNavigate();
  const restaurant = combo.items[0]?.restaurant_id;
  const clash = cartRestaurant !== null && cartRestaurant !== restaurant;
  const blocked =
    combo.items.some((i) => !i.orderable || i.needs_choice) ||
    combo.items.some((i) => i.restaurant_id !== restaurant);

  const addBoth = () => {
    for (const item of combo.items) {
      add(
        { id: item.restaurant_id, name: item.restaurant_name },
        {
          itemId: item.item_id,
          name: item.name,
          priceCents: item.price_cents,
          options: [],
        },
      );
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-2.5">
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 truncate text-sm">
          {combo.items.map((i) => i.name).join(" + ")}
        </p>
        <Money cents={combo.total_cents} />
      </div>
      <p className="mt-0.5 text-[11px] text-slate-500">
        {combo.items[0]?.restaurant_name} · ordered together {combo.orders}×
      </p>
      {clash ? (
        <button
          className="btn-ghost mt-2 w-full text-xs"
          onClick={() => {
            onOpen();
            navigate(`/r/${restaurant}`);
          }}
        >
          Your cart is from another restaurant — open this one
        </button>
      ) : blocked ? (
        <button
          className="btn-ghost mt-2 w-full text-xs"
          onClick={() => {
            onOpen();
            navigate(`/r/${restaurant}`);
          }}
        >
          Open the restaurant to choose options
        </button>
      ) : (
        <button
          data-testid="assistant-add-combo"
          className="btn-ghost mt-2 w-full text-xs"
          onClick={addBoth}
        >
          Add both
        </button>
      )}
    </div>
  );
}

/** A dish the answer named. The price shown is the one the read returned —
 * and checkout still reprices from its own snapshot and refuses on
 * mismatch (ADR-0036), so this is a display, never a promise. */
function AssistantItem({ card, onOpen }: { card: AssistantCard; onOpen: () => void }) {
  const add = useCart((c) => c.add);
  // Derived, not remembered. `clash` was a latch that nothing cleared, so a
  // customer who emptied their cart elsewhere came back to a card still
  // refusing to add — the panel is `fixed` and outside <Routes>, so it does
  // not remount on navigation (B3 review).
  const cartRestaurant = useCart((c) => c.restaurantId);
  const navigate = useNavigate();
  const [tried, setTried] = useState(false);
  const clash = tried && cartRestaurant !== null && cartRestaurant !== card.restaurant_id;

  const addToCart = () => {
    setTried(true);
    add(
      { id: card.restaurant_id, name: card.restaurant_name },
      { itemId: card.item_id, name: card.name, priceCents: card.price_cents, options: [] },
    );
    // One restaurant per order. Rather than silently clearing somebody's
    // cart, `clash` above says so and sends them to the restaurant page.
  };

  return (
    <div
      data-testid="assistant-card"
      className="rounded-lg border border-slate-800 bg-slate-900/60 p-2.5"
    >
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{card.name}</p>
          <p className="truncate text-xs text-slate-500">{card.restaurant_name}</p>
        </div>
        {/* The FLOOR when options are required, because that is the number
            the budget was measured against — showing the base price meant a
            "$4.50" card whose cheapest legal line is $9.50 (B4 review). */}
        {card.needs_choice && card.min_total_cents > card.price_cents ? (
          <span className="whitespace-nowrap text-xs text-slate-400">
            from <Money cents={card.min_total_cents} />
          </span>
        ) : (
          <Money cents={card.price_cents} />
        )}
      </div>
      {clash ? (
        <button
          className="btn-ghost mt-2 w-full text-xs"
          onClick={() => {
            onOpen();
            navigate(`/r/${card.restaurant_id}`);
          }}
        >
          Your cart is from another restaurant — open this one
        </button>
      ) : card.orderable && card.needs_choice ? (
        // A required modifier group cannot be chosen here, and adding it
        // blind builds a cart line the quote endpoint refuses with no UI
        // anywhere to repair it.
        <button
          className="btn-ghost mt-2 w-full text-xs"
          onClick={() => {
            onOpen();
            navigate(`/r/${card.restaurant_id}`);
          }}
        >
          Choose options
        </button>
      ) : card.orderable ? (
        <button data-testid="assistant-add" className="btn-ghost mt-2 w-full text-xs" onClick={addToCart}>
          Add to cart
        </button>
      ) : (
        <p className="mt-2 text-xs text-slate-500">
          {/* Two different reasons, kept apart: a dish that is 86'd is gone
              for now, a shut kitchen is back later. */}
          {!card.available ? "Sold out right now" : "Kitchen closed"}
        </p>
      )}
    </div>
  );
}
