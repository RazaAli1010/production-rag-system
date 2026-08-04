/**
 * The signup password policy, for live feedback in the form.
 *
 * ponytail: deliberately duplicated from backend/app/auth/schemas.py `password_issues` — the
 * server is the one that enforces, this copy only saves the user a round-trip. Edit both together.
 */
export const PASSWORD_RULES: { label: string; ok: (pw: string, local: string) => boolean }[] = [
  { label: "At least 8 characters", ok: (pw) => pw.length >= 8 && pw.length <= 72 },
  { label: "A lowercase letter", ok: (pw) => /[a-z]/.test(pw) },
  { label: "An uppercase letter", ok: (pw) => /[A-Z]/.test(pw) },
  { label: "A number", ok: (pw) => /\d/.test(pw) },
  {
    label: "Not based on your email",
    ok: (pw, local) => local.length < 3 || !pw.toLowerCase().includes(local),
  },
];

/** Unmet rule labels. Empty array == acceptable. */
export function passwordIssues(password: string, email = ""): string[] {
  const local = email.split("@")[0]!.toLowerCase();
  return PASSWORD_RULES.filter((r) => !r.ok(password, local)).map((r) => r.label);
}
