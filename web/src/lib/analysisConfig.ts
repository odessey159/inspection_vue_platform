/**
 * Analysis mode selection policy.
 *
 * - `false` (default): allow choosing Provider / YOLO + Provider without pre-checking
 *   API connectivity; YOLO and LLM failures are reported after analysis runs.
 * - `true`: restore legacy behavior — disable unavailable modes in the UI and fall back
 *   when the configured YOLO service or vision provider is not reachable.
 */
export const REQUIRE_PROVIDER_AVAILABILITY_FOR_SELECTION = false;

/** Default analysis mode shown on a fresh workspace. */
export const DEFAULT_ANALYSIS_MODE = "provider_yolo" as const;
