/** T23 — accessibility (AC-14, AC-40, AC-42, AC-43). */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { setTokens } from "../api/tokens";
import { server } from "../mocks/server";

const renderAt = (route: string) =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>,
  );

describe("axe", () => {
  it("chat has no violations", async () => {
    const { container } = renderAt("/");
    await screen.findByLabelText(/your question/i);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("sources has no violations", async () => {
    const { container } = renderAt("/sources");
    await screen.findByText(/what the assistant reads/i);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("login has no violations", async () => {
    const { container } = renderAt("/login");
    await screen.findByLabelText("Email");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("admin has no violations", async () => {
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
    const { container } = renderAt("/admin");
    await screen.findByText(/service stats/i);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("keyboard-only flow", () => {
  it("asks, opens a citation, and closes it with focus restored (AC-14, AC-41)", async () => {
    const user = userEvent.setup();
    renderAt("/");

    const input = await screen.findByLabelText(/your question/i);
    await user.click(input);
    await user.keyboard("__happy probation se kaise nikalta hoon{Enter}");

    const chip = await screen.findByRole("button", { name: /source 1/i }, { timeout: 5000 });
    chip.focus();
    await user.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // Focus moved into the sheet, not left behind on the chip.
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(chip);
  });
});

describe("live regions (AC-40)", () => {
  it("keeps the stage trail and the answer in separate polite regions", async () => {
    const user = userEvent.setup();
    const { container } = renderAt("/");

    await user.type(screen.getByLabelText(/your question/i), "__happy probation{Enter}");

    await waitFor(() => {
      const regions = container.querySelectorAll('[aria-live="polite"]');
      expect(regions.length).toBeGreaterThanOrEqual(2);
    });
  });
});
