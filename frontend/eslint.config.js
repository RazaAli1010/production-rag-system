import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "src/api/generated.ts"] }, // generated.ts is codegen output, never hand-edited
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { ecmaVersion: 2022, sourceType: "module" },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      // The allowlist markdown renderer is what makes the localStorage refresh-token tradeoff
      // defensible (requirements §4). Keep raw HTML out of the codebase.
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message:
            "No raw HTML. Answers render through chat/Markdown.tsx's allowlist — see requirements §4.",
        },
      ],
    },
  },
);
