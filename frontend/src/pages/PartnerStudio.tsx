import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveDraft, draftBusinessCopy, draftMenuItems, getFeedbackDigest,
  listDrafts, patchItem, rejectDraft, replayDraft, requestFeedbackSummary,
} from "../api/client";
import type { ContentDraft, FeedbackDigest } from "../api/types";
import { ErrorNote, Note, Spinner } from "../components/ui";

/**
 * The content studio (B6).
 *
 * One screen for the whole lifecycle, because the lifecycle is one thing:
 * a draft is asked for, written, read by a person, and then published or
 * declined. Splitting the asking from the reviewing would hide the only
 * state that matters — how many drafts are sitting there waiting for a
 * human, which is the number FR-93 turns into a queue of real decisions.
 *
 * Parked jobs are in the same list as everything else. That is the payoff
 * for making the dead-letter queue a ROW: the restaurant whose copy never
 * arrived sees that it did not and why, and can put it back in the queue
 * without anyone opening a broker console.
 */

const KIND_LABEL: Record<ContentDraft["kind"], string> = {
  menu_item: "Menu description",
  promotion: "Promotion",
  engagement: "Customer message",
  feedback_summary: "Feedback summary",
};

const STATUS_TONE: Record<ContentDraft["status"], string> = {
  queued: "border-slate-700 text-slate-400",
  drafted: "border-sky-800 bg-sky-950/40 text-sky-300",
  parked: "border-amber-900 bg-amber-950/50 text-amber-300",
  published: "border-emerald-900 bg-emerald-950/40 text-emerald-300",
  rejected: "border-slate-700 text-slate-500",
};

function StatusTag({ status }: { status: ContentDraft["status"] }) {
  return (
    <span
      data-testid="draft-status"
      className={`rounded-lg border px-2 py-0.5 text-[11px] uppercase tracking-wide ${STATUS_TONE[status]}`}
    >
      {status}
    </span>
  );
}

export default function PartnerStudio() {
  // No scope prop. Drafting is claim-scoped on the server, and each draft
  // row names the restaurant its copy is for — so neither asking nor
  // publishing depends on which branch chip the dashboard has selected.
  const queryClient = useQueryClient();
  const [ask, setAsk] = useState("");

  const drafts = useQuery({
    queryKey: ["drafts"],
    queryFn: () => listDrafts(),
    // Queued drafts finish in the background, so the list is polled while
    // any of them is still moving — and stops when none is.
    refetchInterval: (query) =>
      query.state.data?.drafts.some((d) => d.status === "queued") ? 4000 : false,
  });
  const digest = useQuery({ queryKey: ["feedback-digest"], queryFn: getFeedbackDigest, retry: false });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["drafts"] });
    queryClient.invalidateQueries({ queryKey: ["feedback-digest"] });
  };

  const askFor = useMutation({
    mutationFn: (kind: "promotions" | "engagement") => draftBusinessCopy(kind, ask),
    onSuccess: () => { setAsk(""); invalidate(); },
  });
  const summarise = useMutation({ mutationFn: requestFeedbackSummary, onSuccess: invalidate });

  if (drafts.isLoading) return <Spinner />;

  // Summaries are shown in the feedback panel, where they mean something.
  // In this list they would be empty cards: a summary cannot be published
  // or rejected, so there is nothing to decide and nothing to render but a
  // status tag. A parked one still belongs here — a summary that failed is
  // a job someone may want to retry.
  const rows = (drafts.data?.drafts ?? []).filter(
    (d) => d.kind !== "feedback_summary" || d.status === "parked",
  );
  const waiting = rows.filter((d) => d.status === "drafted").length;

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex items-baseline justify-between">
          <b className="text-sm">Ask for copy</b>
          {waiting > 0 && (
            <span className="text-xs text-sky-300">
              {waiting} draft{waiting === 1 ? "" : "s"} waiting for you
            </span>
          )}
        </div>
        <textarea
          className="input h-16 w-full text-sm"
          data-testid="studio-ask"
          placeholder="Describe an offer, or a message for customers who haven't ordered in a while…"
          value={ask}
          onChange={(e) => setAsk(e.target.value)}
        />
        <div className="flex flex-wrap gap-2">
          <button
            className="btn-primary"
            disabled={!ask.trim() || askFor.isPending}
            onClick={() => askFor.mutate("promotions")}
          >
            Draft a promotion
          </button>
          <button
            className="btn-ghost"
            disabled={!ask.trim() || askFor.isPending}
            onClick={() => askFor.mutate("engagement")}
          >
            Draft a customer message
          </button>
        </div>
        {/* 422 here is the floor, not a failure: a restaurant with a
            handful of orders has no aggregate worth writing from, and copy
            built on one would be a claim dressed as a statistic. */}
        <ErrorNote error={askFor.error} />
      </div>

      <MenuDrafts onDone={invalidate} />

      <FeedbackPanel
        digest={digest.data}
        loading={digest.isLoading}
        failed={digest.error}
        onSummarise={() => summarise.mutate()}
        pending={summarise.isPending}
        error={summarise.error}
      />

      <section className="space-y-2">
        <b className="text-sm">Drafts</b>
        {rows.length === 0 && (
          <Note tone="info">Nothing drafted yet. Ask for some copy above.</Note>
        )}
        {rows.map((draft) => (
          <DraftCard key={draft.draft_id} draft={draft} onDone={invalidate} />
        ))}
      </section>
    </div>
  );
}

/** Asking for menu copy, which needs dishes rather than a sentence. */
function MenuDrafts({ onDone }: { onDone: () => void }) {
  const [category, setCategory] = useState("");
  const ask = useMutation({
    mutationFn: () => draftMenuItems({ category: category.trim() }),
    onSuccess: () => { setCategory(""); onDone(); },
  });
  return (
    <div className="card space-y-2">
      <b className="text-sm">Describe a menu section</b>
      <div className="flex gap-2">
        <input
          className="input flex-1 text-sm"
          data-testid="studio-category"
          placeholder="Category name, e.g. Mains"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        />
        <button
          className="btn-primary"
          disabled={!category.trim() || ask.isPending}
          onClick={() => ask.mutate()}
        >
          Draft
        </button>
      </div>
      {ask.data && (
        <p className="text-xs text-slate-400">
          Queued {ask.data.queued}
          {/* A COUNT, never a list of which: that difference is what stops
              a probe mapping another restaurant's menu one id at a time. */}
          {ask.data.skipped > 0 && ` · ${ask.data.skipped} not found on this menu`}
        </p>
      )}
      <p className="text-[11px] text-slate-500">
        Writes from each dish's name, tags, category and cuisine — never its price.
        {" Menu copy is published to the base menu, which every branch inherits."}
      </p>
      <ErrorNote error={ask.error} />
    </div>
  );
}

function DraftCard({ draft, onDone }: { draft: ContentDraft; onDone: () => void }) {
  const [edited, setEdited] = useState<string | null>(null);
  const text = edited ?? draft.content ?? "";

  /**
   * Publish, then record — in that order, and both from here.
   *
   * The menu write is Catalog's ORDINARY item PATCH with this admin's own
   * token (FR-93), which is also what re-embeds the dish through the
   * existing pipeline. The assistant is told afterwards. If the second
   * call fails the draft stays `drafted`, so the admin sees work still to
   * do rather than a row claiming a publication that never happened.
   */
  const publish = useMutation({
    mutationFn: async () => {
      if (draft.kind === "menu_item" && draft.target_id) {
        // The draft row's OWN restaurant, not the selected scope and not
        // the brand. Catalog resolves an item by exact owner: a base item
        // belongs to the brand, a branch-local one to the branch
        // (ADR-0028). This first guessed the branch (base items 404'd),
        // then guessed the brand (local items 404'd); the row has known
        // all along which menu the copy is for.
        //
        // The same call the Menu tab makes to edit a description by hand —
        // FR-93's "ordinary Catalog write path" is ordinary, and it is
        // what re-embeds the dish through the existing pipeline.
        await patchItem(draft.restaurant_id, draft.target_id, { description: text });
      }
      return approveDraft(draft.draft_id, text);
    },
    onSuccess: onDone,
  });
  const decline = useMutation({ mutationFn: () => rejectDraft(draft.draft_id), onSuccess: onDone });
  const retry = useMutation({ mutationFn: () => replayDraft(draft.draft_id), onSuccess: onDone });

  const subjectName = (draft.subject?.name as string | undefined) ?? draft.target_id ?? "";

  return (
    <div className="card space-y-2" data-testid="draft-card">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm">
          <b>{KIND_LABEL[draft.kind]}</b>
          {subjectName && <span className="text-slate-400"> — {subjectName}</span>}
        </span>
        <StatusTag status={draft.status} />
      </div>

      {draft.request && (
        <p className="text-xs italic text-slate-500">You asked: {draft.request}</p>
      )}

      {draft.status === "queued" && <p className="text-sm text-slate-400">Writing…</p>}

      {draft.status === "parked" && (
        <>
          {/* The reason lives on the row, which is why this screen can show
              it at all — a broker DLQ would have it in a console nobody
              here can open. */}
          <Note tone="warn">{draft.error ?? "This one could not be written."}</Note>
          <button className="btn-ghost" disabled={retry.isPending} onClick={() => retry.mutate()}>
            {retry.isPending ? "Requeuing…" : "Try again"}
          </button>
          <ErrorNote error={retry.error} />
        </>
      )}

      {draft.status === "drafted" && draft.kind !== "feedback_summary" && (
        <>
          <textarea
            className="input h-20 w-full text-sm"
            data-testid="draft-text"
            value={text}
            onChange={(e) => setEdited(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            <button
              className="btn-primary"
              data-testid="draft-approve"
              disabled={!text.trim() || publish.isPending}
              onClick={() => publish.mutate()}
            >
              {publish.isPending ? "Publishing…" : "Approve & publish"}
            </button>
            <button
              className="btn-ghost"
              disabled={decline.isPending}
              onClick={() => decline.mutate()}
            >
              Reject
            </button>
          </div>
          <p className="text-[11px] text-slate-500">
            Nothing is published until you press approve. Read it first — it was written by a
            model from your own menu, and it can be wrong.
          </p>
          <ErrorNote error={publish.error ?? decline.error} />
        </>
      )}

      {draft.status === "published" && (
        <>
          <p className="text-sm">{draft.published_content}</p>
          {draft.published_content !== draft.content && (
            <p className="text-[11px] text-slate-500">
              You edited this before publishing. The original is kept.
            </p>
          )}
        </>
      )}

      {draft.status === "rejected" && (
        <p className="text-sm text-slate-500 line-through">{draft.content}</p>
      )}

      {draft.model && (
        <p className="text-[11px] text-slate-600">Written by {draft.model}</p>
      )}
    </div>
  );
}

/** The feedback digest: rows always, a summary only when there is enough. */
function FeedbackPanel({
  digest, loading, failed, onSummarise, pending, error,
}: {
  digest: FeedbackDigest | undefined;
  loading: boolean;
  failed: unknown;
  onSummarise: () => void;
  pending: boolean;
  error: unknown;
}) {
  if (loading) return null;
  // Say so. Returning null deleted the whole "What customers said" panel —
  // counts, summary, every review — with no error and no spinner, which
  // reads as "you have no feedback". That is precisely what the API client
  // raises UpstreamUnavailable to prevent, undone at the last step.
  if (failed) {
    return (
      <div className="card space-y-2">
        <b className="text-sm">What customers said</b>
        <Note tone="warn">
          Reviews are unavailable right now — this is a problem on our side, not an empty
          inbox. Try again in a moment.
        </Note>
      </div>
    );
  }
  if (!digest) return null;
  const { counts, can_summarise: canSummarise, summary, feedback } = digest;

  return (
    <div className="card space-y-3">
      <div className="flex items-baseline justify-between">
        <b className="text-sm">What customers said</b>
        <span className="text-xs text-slate-400" data-testid="feedback-counts">
          {counts.reviews} review{counts.reviews === 1 ? "" : "s"}
          {counts.reviews > 0 && ` · ${counts.average_rating} ★ average`}
        </span>
      </div>

      {summary ? (
        <div className="space-y-2" data-testid="feedback-summary">
          <ul className="list-inside list-disc text-sm text-slate-300">
            {summary.themes.map((theme) => (
              <li key={theme}>{theme}</li>
            ))}
          </ul>
          {summary.quotes.length > 0 && (
            <div className="space-y-1">
              {summary.quotes.map((quote) => (
                /* Every one of these was checked verbatim against a real
                   review before the summary was stored (FR-92). */
                <p key={quote} className="border-l-2 border-slate-700 pl-2 text-xs italic text-slate-400">
                  “{quote}”
                </p>
              ))}
            </div>
          )}
          <p className="text-[11px] text-slate-600">
            Themes and quotes written by {summary.model}; every quote is taken word for word from
            a real review. The numbers above are counted, not written.
          </p>
        </div>
      ) : (
        <Note tone="info">
          {canSummarise
            ? "No summary yet."
            : "Not enough written reviews to find themes in yet — they are listed below."}
        </Note>
      )}

      {canSummarise && (
        <button className="btn-ghost" disabled={pending} onClick={onSummarise}>
          {pending ? "Summarising…" : summary ? "Refresh summary" : "Summarise"}
        </button>
      )}
      <ErrorNote error={error} />

      <div className="space-y-1">
        {feedback.slice(0, 8).map((row) => (
          <div key={row.order_id} className="flex gap-2 text-xs">
            <span className="text-amber-400">{"★".repeat(row.rating)}</span>
            <span className="text-slate-400">{row.comment ?? <em>no comment</em>}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
