import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

/** Browser-side MSW, enabled with VITE_ENABLE_MOCKS=true for design work without a backend. */
export const worker = setupWorker(...handlers);
