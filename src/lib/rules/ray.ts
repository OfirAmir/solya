import type { RayBand } from "./types";

/** Frozen v1 weights from the handover (section 2.4). Do not retune here. */
export const RAY_WEIGHTS = {
  performance: 0.4,
  health: 0.35,
  environment: 0.25,
} as const;

/**
 * Cloud cover lowers the environment term by 0.4 points per percent.
 * 52.3% cloud then costs about 5.2 RAY points (0.25 * 52.3 * 0.4).
 */
export const CLOUD_ENVIRONMENT_PER_PERCENT = 0.4;

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** Actual ÷ expected, capped at 100. Missing inputs stay null, never zero. */
export function performanceScore(actualWh: number | null, expectedWh: number | null): number | null {
  if (actualWh == null || expectedWh == null || expectedWh <= 0) return null;
  return clamp((actualWh / expectedWh) * 100, 0, 100);
}

/**
 * An open alert is a fault, not a subtraction of its production share.
 * Measured zero is a fault. Communication loss has no health score.
 */
export function healthScore(input: {
  communicationLost: boolean;
  measuredZero: boolean;
  openAlertCount: number;
}): number | null {
  if (input.communicationLost) return null;
  if (input.measuredZero) return 20;
  return clamp(100 - input.openAlertCount * 25, 0, 100);
}

export function environmentScore(input: {
  includeWeather: boolean;
  cloudCoverPercent: number | null;
  soilingIndex: number;
}): number {
  const weather =
    input.includeWeather && input.cloudCoverPercent != null
      ? input.cloudCoverPercent * CLOUD_ENVIRONMENT_PER_PERCENT
      : 0;
  return clamp(100 - weather - input.soilingIndex, 0, 100);
}

export function composeRay(performance: number, health: number, environment: number): number {
  const raw =
    RAY_WEIGHTS.performance * performance +
    RAY_WEIGHTS.health * health +
    RAY_WEIGHTS.environment * environment;
  return Math.round(raw);
}

export function rayBand(ray: number): RayBand {
  if (ray >= 90) return "green";
  if (ray >= 80) return "orange";
  if (ray >= 60) return "yellow";
  return "red";
}

export function rayBandLabel(band: RayBand): string | null {
  if (band === "red") return "תקלה - חייב טיפול";
  return null;
}

/** kWp-weighted average. Sites without a trusted installed kW are left out of the weights. */
export function weightedRay(
  parts: { ray: number | null; installedKwDc: number | null }[],
): number | null {
  const scored = parts.filter((p) => p.ray != null);
  if (scored.length === 0) return null;
  const weighted = scored.filter((p) => p.installedKwDc != null && p.installedKwDc > 0);
  if (weighted.length !== scored.length) {
    const avg = scored.reduce((sum, p) => sum + (p.ray as number), 0) / scored.length;
    return Math.round(avg);
  }
  const weight = weighted.reduce((sum, p) => sum + (p.installedKwDc as number), 0);
  const acc = weighted.reduce(
    (sum, p) => sum + (p.ray as number) * (p.installedKwDc as number),
    0,
  );
  return Math.round(acc / weight);
}
