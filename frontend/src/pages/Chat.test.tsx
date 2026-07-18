/**
 * T14–T18 — the chat screen against MSW (AC-15, AC-16, AC-19, AC-20, AC-21, AC-25, AC-28, AC-35).
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { __resetClient } from "../api/client";
import { setTokens } from "../api/tokens";
import { server } from "../mocks/server";
import { App } from "../App";

const renderApp = (route = "/") =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>,
  );

const askFor = async (user: ReturnType<typeof userEvent.setup>, question: string) => {
  await user.type(screen.getByLabelText(/your question/i), question);
  await user.click(screen.getByRole("button", { name: "Ask" }));
};

beforeEach(() => {
  __resetClient();
});

describe("anonymous chat", () => {
  it("creates one session and reuses it across turns (AC-15, AC-16)", async () => {
    const created: unknown[] = [];
    const asked: { session_id?: string }[] = [];
    server.use(
      http.post("/api/sessions", () => {
        created.push(1);
        return HttpResponse.json(
          {
            id: "11111111-1111-1111-1111-111111111111",
            title: null,
            total_tokens: 0,
            created_at: new Date().toISOString(),
            last_active_at: new Date().toISOString(),
          },
          { status: 201 },
        );
      }),
    );
    const original = server.listHandlers();
    void original;
    server.events.on("request:start", async ({ request }) => {
      if (request.method === "POST" && request.url.includes("/api/ask")) {
        asked.push((await request.clone().json()) as { session_id?: string });
      }
    });

    const user = userEvent.setup();
    renderApp();

    await askFor(user, "__happy probation se kaise nikalta hoon");
    await screen.findByText(/worked/i, {}, { timeout: 5000 });

    await waitFor(() => expect(created).toHaveLength(1));
    expect(asked[0]?.session_id).toBe("11111111-1111-1111-1111-111111111111");
  });

  it("streams stages, then the answer, then a citation chip", async () => {
    const user = userEvent.setup();
    renderApp();

    await askFor(user, "__happy probation se kaise nikalta hoon");

    // The trail collapses into the receipt once tokens start.
    const receipt = await screen.findByRole("button", { name: /worked/i }, { timeout: 5000 });
    expect(receipt).toBeInTheDocument();
    // The receipt appears at the FIRST token; citations only arrive after the last one.
    expect(
      await screen.findByRole("button", { name: /source 1/i }, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(screen.getByText(/CGPA falls below 2\.00/i)).toBeInTheDocument();
  });

  it("opens the citation panel from a chip (AC-12)", async () => {
    const user = userEvent.setup();
    renderApp();
    await askFor(user, "__happy probation");
    await user.click(await screen.findByRole("button", { name: /source 1/i }, { timeout: 5000 }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("link", { name: /open official document/i })).toHaveAttribute(
      "href",
      expect.stringContaining("#page=112"),
    );
  });

  it("renders a refusal without the error affordance (AC-24)", async () => {
    const user = userEvent.setup();
    renderApp();
    await askFor(user, "__refusal what is the wifi password");

    expect(await screen.findByText(/not in these documents/i, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("keeps the partial answer and offers a retry when the stream drops (AC-27)", async () => {
    const user = userEvent.setup();
    renderApp();
    await askFor(user, "__disconnect fee refund");

    expect(await screen.findByText(/Fee refunds are issued/i, {}, { timeout: 5000 })).toBeInTheDocument();
    // The retry appears only once the stream has actually settled as interrupted.
    expect(
      await screen.findByRole("button", { name: /try again/i }, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it("counts down and locks the composer on 429 (AC-25)", async () => {
    const user = userEvent.setup();
    renderApp();
    await askFor(user, "__429 too many questions");

    expect(await screen.findByText(/try again in \d+s/i, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByLabelText(/your question/i)).toBeDisabled();
  });

  it("locks with non-error copy on 409 session_busy (AC-28)", async () => {
    const user = userEvent.setup();
    renderApp();
    await askFor(user, "__409 still working");

    expect(
      await screen.findByText(/finishing your last question/i, {}, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("shows the log-in hint and never calls the session list (AC-20)", async () => {
    const listed = vi.fn();
    server.use(
      http.get("/api/sessions", () => {
        listed();
        return HttpResponse.json([]);
      }),
    );
    renderApp();

    expect(await screen.findByText(/log in to keep your chats/i)).toBeInTheDocument();
    await waitFor(() => expect(listed).not.toHaveBeenCalled());
  });

  it("offers example questions in both registers on the empty state", () => {
    renderApp();
    expect(screen.getByText("probation se kaise nikalta hoon?")).toBeInTheDocument();
    expect(screen.getByText("What CGPA puts me on probation?")).toBeInTheDocument();
  });
});

describe("authenticated chat", () => {
  beforeEach(() => setTokens("access", "refresh"));

  it("lists saved sessions newest first and resumes one (AC-19, AC-21)", async () => {
    const user = userEvent.setup();
    renderApp();

    // Two controls mention the title: the resume button and its delete control. Take the first.
    const [resume] = await screen.findAllByRole("button", { name: /probation and cgpa/i });
    // The untitled older session falls back to a label rather than rendering blank.
    expect(screen.getByText("Untitled chat")).toBeInTheDocument();

    await user.click(resume!);

    expect(await screen.findByText(/raise your CGPA to 2\.00/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /source 1/i })).toBeInTheDocument();
  });

  it("deletes a session after confirming (AC-22)", async () => {
    const user = userEvent.setup();
    const deleted: string[] = [];
    server.use(
      http.delete("/api/sessions/:id", ({ params }) => {
        deleted.push(params.id as string);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp();

    await user.click(await screen.findByRole("button", { name: /delete probation and cgpa/i }));
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleted).toHaveLength(1));
    expect(screen.queryAllByRole("button", { name: /probation and cgpa/i })).toHaveLength(0);
  });
});

describe("admin route guard (AC-35)", () => {
  it("redirects a student away from /admin and hides the nav entry", async () => {
    setTokens("access", "refresh");
    renderApp("/admin");

    // /api/auth/me returns role "student" by default.
    expect(await screen.findByLabelText(/your question/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /stats/i })).not.toBeInTheDocument();
  });

  it("renders the stats cards for an admin", async () => {
    setTokens("access", "refresh");
    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({
          id: "99999999-9999-9999-9999-999999999999",
          email: "admin@pu.edu.pk",
          role: "admin",
          is_active: true,
          created_at: "2026-01-04T10:00:00Z",
        }),
      ),
    );
    renderApp("/admin");

    expect(await screen.findByText(/service stats/i)).toBeInTheDocument();
    // "1,284" is both the request count and several flag-usage counts, so scope to the stat tile.
    const requests = (await screen.findByText("Requests")).closest("div")!;
    expect(within(requests).getByText("1,284")).toBeInTheDocument();
    expect(screen.getByText("31.0%")).toBeInTheDocument(); // cache_hit_rate
    expect(screen.getByText("$1.87")).toBeInTheDocument();
    expect(screen.getByText("p95 4300ms")).toBeInTheDocument();
  });
});
