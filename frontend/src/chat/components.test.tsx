/** T9/T11/T12/T13 — component behaviour (AC-3, AC-4, AC-5, AC-8, AC-12, AC-13, AC-14, AC-24). */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Citation } from "../api/types";
import { CitationPanel, pageLabel, sourceHref } from "./CitationPanel";
import { Composer } from "./Composer";
import { Markdown } from "./Markdown";
import { RefusalCard } from "./RefusalCard";
import { StampTrail, WorkedChip } from "./StampTrail";
import type { TrailStage } from "./types";

const cite = (o: Partial<Citation> = {}): Citation => ({
  chunk_id: "pu-calendar-2023:41",
  doc_id: "pu-calendar-2023",
  title: "PU Calendar, Volume II",
  section_heading: "Probation",
  page_start: 112,
  page_end: 112,
  url: "https://pu.edu.pk/calendar/vol-ii.pdf",
  quote: "A student whose CGPA falls below 2.00 shall be placed on probation.",
  ...o,
});

describe("StampTrail", () => {
  const stages: TrailStage[] = [
    { stage: "rewriting", status: "skipped", ms: null },
    { stage: "searching", status: "done", ms: 380 },
    { stage: "consulting_registrar", status: "done", ms: 55 },
    { stage: "generating", status: "started", ms: null },
  ];

  it("renders stages in arrival order with their timings", () => {
    render(<StampTrail stages={stages} />);
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items[0]).toContain("Rewriting your question");
    expect(items[1]).toContain("Searching documents");
    expect(items[1]).toContain("380ms");
  });

  it("renders an unknown stage id rather than dropping it (AC-4)", () => {
    render(<StampTrail stages={stages} />);
    expect(screen.getByText(/consulting registrar/i)).toBeInTheDocument();
  });

  it("announces only the latest stage label, never its timing", () => {
    const { container } = render(<StampTrail stages={stages} />);
    const live = container.querySelector('[aria-live="polite"]')!;
    expect(live.textContent).toBe("Writing the answer");
    expect(live.textContent).not.toMatch(/\d+ms/);
  });

  it("expands a traced stage to show what it produced", async () => {
    // Reranking is the stage whose effect is invisible from timings alone: the trace has to show
    // the cross-encoder MOVING a passage, not just that it took 120ms.
    const user = userEvent.setup();
    const traced: TrailStage[] = [
      {
        stage: "reranking",
        status: "done",
        ms: 120,
        detail: {
          n_candidates: 2,
          kept: 2,
          before: [
            { chunk_id: "d:1", title: "Weak match", section: null, page: 3, text: "…", score: 0.4 },
            { chunk_id: "d:2", title: "Strong match", section: null, page: 9, text: "…", score: 0.3 },
          ],
          after: [
            {
              chunk_id: "d:2",
              title: "Strong match",
              section: null,
              page: 9,
              text: "…",
              score: 0.98,
              moved: 1,
            },
          ],
        },
      },
    ];
    render(<StampTrail stages={traced} />);
    const stamp = screen.getByRole("button", { name: /reranking results/i });
    expect(screen.queryByText(/cross-encoder/i)).not.toBeInTheDocument();

    await user.click(stamp);
    expect(stamp).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/after \(cross-encoder\)/i)).toBeInTheDocument();
    expect(screen.getByText("▲1")).toBeInTheDocument(); // promoted one place
  });

  it("leaves an untraced stage as inert text, not a dead control", () => {
    render(<StampTrail stages={stages} />);
    expect(screen.queryByRole("button", { name: /searching documents/i })).not.toBeInTheDocument();
  });
});

describe("WorkedChip", () => {
  const stages: TrailStage[] = [
    { stage: "searching", status: "done", ms: 380 },
    { stage: "generating", status: "done", ms: 890 },
  ];

  it("summarises as a receipt and expands to the timings (AC-5)", async () => {
    const user = userEvent.setup();
    render(<WorkedChip stages={stages} latencyMs={2140} />);
    const chip = screen.getByRole("button", { name: /worked 2\.1s/i });
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    await user.click(chip);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(chip).toHaveAttribute("aria-expanded", "true");
  });
});

describe("Markdown citations", () => {
  const open = vi.fn();

  it("turns a resolvable [n] into a chip", () => {
    render(<Markdown text="Probation applies [1]." citations={[cite()]} onOpenCitation={open} />);
    expect(screen.getByRole("button", { name: /source 1/i })).toBeInTheDocument();
  });

  it("leaves an unresolvable [n] as plain text (AC-8)", () => {
    render(<Markdown text="See [9] for detail." citations={[cite()]} onOpenCitation={open} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/\[9\]/)).toBeInTheDocument();
  });

  it("holds back a partial marker while streaming", () => {
    const { container } = render(
      <Markdown text="Probation applies [1" citations={[cite()]} streaming onOpenCitation={open} />,
    );
    // No flash of a literal "[1" that would reflow into a chip a frame later.
    expect(container.textContent).toBe("Probation applies");
  });
});

describe("citation links (AC-13)", () => {
  it("deep-links to the page when there is one", () => {
    expect(sourceHref(cite())).toBe("https://pu.edu.pk/calendar/vol-ii.pdf#page=112");
  });
  it("links plainly when there is no page", () => {
    expect(sourceHref(cite({ page_start: null }))).toBe("https://pu.edu.pk/calendar/vol-ii.pdf");
  });
  it("offers no link at all when the citation carries no url", () => {
    expect(sourceHref(cite({ url: null }))).toBeNull();
  });
  it("labels a page range", () => {
    expect(pageLabel(cite({ page_end: 114 }))).toBe("Pages 112–114");
    expect(pageLabel(cite())).toBe("Page 112");
    expect(pageLabel(cite({ page_start: null }))).toBeNull();
  });
});

describe("CitationPanel", () => {
  it("shows the source and closes on Escape, restoring focus (AC-14)", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <>
        <button type="button">opener</button>
        <CitationPanel citation={cite()} index={1} onClose={onClose} />
      </>,
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/PU Calendar, Volume II/)).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: /open official document/i })).toHaveAttribute(
      "href",
      "https://pu.edu.pk/calendar/vol-ii.pdf#page=112",
    );
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("renders no link for a suggestion citation", () => {
    render(<CitationPanel citation={cite({ url: null })} index={1} onClose={vi.fn()} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("Composer", () => {
  it("keeps Ask disabled below the 3-character floor (AC-2)", async () => {
    const user = userEvent.setup();
    render(<Composer onAsk={vi.fn()} />);
    const ask = screen.getByRole("button", { name: "Ask" });
    expect(ask).toBeDisabled();
    await user.type(screen.getByLabelText(/your question/i), "hi");
    expect(ask).toBeDisabled();
    expect(screen.getByText(/at least 3 characters/i)).toBeInTheDocument();
  });

  it("marks the counter over the 500-character ceiling and blocks sending", async () => {
    const user = userEvent.setup();
    render(<Composer onAsk={vi.fn()} />);
    await user.click(screen.getByLabelText(/your question/i));
    await user.paste("x".repeat(501));
    expect(screen.getByText("501/500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });

  it("submits on Enter and omits namespace when All is selected (AC-11, AC-41)", async () => {
    const user = userEvent.setup();
    const onAsk = vi.fn();
    render(<Composer onAsk={onAsk} />);
    await user.type(screen.getByLabelText(/your question/i), "probation se kaise nikalta hoon{Enter}");
    expect(onAsk).toHaveBeenCalledWith("probation se kaise nikalta hoon", undefined);
  });

  it("sends the namespace once a source chip is chosen", async () => {
    const user = userEvent.setup();
    const onAsk = vi.fn();
    render(<Composer onAsk={onAsk} />);
    await user.click(screen.getByRole("button", { name: "HEC" }));
    await user.type(screen.getByLabelText(/your question/i), "plagiarism policy{Enter}");
    expect(onAsk).toHaveBeenCalledWith("plagiarism policy", "hec");
  });

  it("shows the lock note instead of the counter while disabled", () => {
    render(<Composer onAsk={vi.fn()} disabled lockNote="Finishing your last question…" />);
    expect(screen.getByText(/finishing your last question/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/your question/i)).toBeDisabled();
  });
});

describe("RefusalCard", () => {
  it("reads as a valid answer, not a failure (AC-24)", () => {
    render(
      <RefusalCard
        reason="low_retrieval_confidence"
        suggestions={[cite({ title: "PU Fee Refund Schedule", url: null })]}
      />,
    );
    expect(screen.getByText(/not in these documents/i)).toBeInTheDocument();
    // The machine token must never reach the student.
    expect(screen.queryByText(/low_retrieval_confidence/)).not.toBeInTheDocument();
    expect(screen.getByText(/didn't actually cover this|matched this closely enough/i)).toBeInTheDocument();
    expect(screen.getByText(/you might check/i)).toBeInTheDocument();
    expect(screen.getByText(/PU Fee Refund Schedule/)).toBeInTheDocument();
    // No retry affordance: a refusal is not something to retry.
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });
});
