import type { AnswerMeta, Citation, StageEvent } from "../api/types";
import type { ApiError } from "../api/errors";

/**
 * `refused` and `interrupted` are deliberately distinct from an error:
 *  - `refused` is a VALID answer. The pipeline searched and found nothing above the confidence
 *    threshold. Rendering it as a failure would teach students to distrust a system that is
 *    behaving exactly as designed.
 *  - `interrupted` means partial text survived. Never discard it.
 */
export type TurnStatus = "streaming" | "done" | "refused" | "interrupted" | "failed";

export interface TrailStage {
  stage: string;
  status: StageEvent["status"];
  ms: number | null;
}

export interface Turn {
  id: string;
  question: string;
  /** Grows token by token. Preserved on every failure path. */
  answer: string;
  stages: TrailStage[];
  citations: Citation[];
  meta?: AnswerMeta;
  status: TurnStatus;
  error?: ApiError;
  /** Set true by the first token: the live trail becomes the `worked for Ns` receipt. */
  trailCollapsed: boolean;
  namespace?: "pu" | "hec";
}

export const totalStageMs = (stages: TrailStage[]): number =>
  stages.reduce((sum, s) => sum + (s.ms ?? 0), 0);
