/**
 * vitest-axe ships its matcher implementation but not an ambient augmentation for Vitest's
 * `Assertion` interface, so `toHaveNoViolations()` typechecks as missing. Declared here once.
 *
 * The empty-extension interfaces below are the mechanism of declaration merging, not an oversight,
 * so `no-empty-object-type` is off for this file only.
 */
/* eslint-disable @typescript-eslint/no-empty-object-type */
import "vitest";

interface AxeMatchers<R = unknown> {
  toHaveNoViolations(): R;
}

declare module "vitest" {
  interface Assertion<T = unknown> extends AxeMatchers<T> {}
  interface AsymmetricMatchersContaining extends AxeMatchers {}
}
