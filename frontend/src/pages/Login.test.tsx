/** Signup: the strength rule is enforced before submit, and a real server error is shown. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { __resetClient } from "../api/client";
import { App } from "../App";
import { passwordIssues } from "../auth/passwordRules";

const renderAt = (route: string) =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>,
  );

const renderApp = () => renderAt("/register");

beforeEach(() => __resetClient());

describe("passwordIssues", () => {
  it("names every unmet rule and passes a strong one", () => {
    expect(passwordIssues("abc")).toEqual([
      "At least 8 characters",
      "An uppercase letter",
      "A number",
    ]);
    expect(passwordIssues("Testpass123", "student@pu.edu.pk")).toEqual([]);
    expect(passwordIssues("Student1234", "student@pu.edu.pk")).toEqual([
      "Not based on your email",
    ]);
  });
});

describe("register form", () => {
  it("keeps submit disabled until the password meets every rule", async () => {
    const user = userEvent.setup();
    renderApp();
    // The route is lazy, so the first query has to wait for the chunk.
    await user.type(await screen.findByLabelText(/email/i), "new@pu.edu.pk");
    const submit = screen.getByRole("button", { name: /create account/i });

    await user.type(screen.getByLabelText(/password/i), "probation");
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/password/i), "X1");
    expect(submit).toBeEnabled();
  });

  it("shows the server's message when the email is already registered", async () => {
    const user = userEvent.setup();
    renderApp();

    await user.type(await screen.findByLabelText(/email/i), "taken@pu.edu.pk");
    await user.type(screen.getByLabelText(/password/i), "Probation123");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/email already registered/i);
  });
});

describe("password reset", () => {
  it("offers the flow from the login page", async () => {
    renderAt("/login");
    expect(await screen.findByRole("link", { name: /forgot password/i })).toHaveAttribute(
      "href",
      "/forgot-password",
    );
  });

  it("confirms without revealing whether the address exists", async () => {
    const user = userEvent.setup();
    renderAt("/forgot-password");

    await user.type(await screen.findByLabelText(/email/i), "whoever@pu.edu.pk");
    await user.click(screen.getByRole("button", { name: /send reset link/i }));

    expect(await screen.findByText(/if an account exists/i)).toBeInTheDocument();
  });

  it("holds submit until the new password is strong, then returns to log in", async () => {
    const user = userEvent.setup();
    renderAt("/reset-password?token=good");

    await user.type(await screen.findByLabelText(/new password/i), "weakpass");
    const submit = screen.getByRole("button", { name: /set new password/i });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/new password/i), "X1");
    await user.click(submit);

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument();
  });

  it("says so when the link has expired", async () => {
    const user = userEvent.setup();
    renderAt("/reset-password?token=expired");

    await user.type(await screen.findByLabelText(/new password/i), "Newpassword1");
    await user.click(screen.getByRole("button", { name: /set new password/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid or has expired/i);
  });

  it("does not show a password form when the link carries no token", async () => {
    renderAt("/reset-password");

    expect(await screen.findByRole("alert")).toHaveTextContent(/missing its token/i);
    expect(screen.queryByLabelText(/new password/i)).not.toBeInTheDocument();
  });
});
