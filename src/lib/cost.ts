/** Conservative dollar cost if credits are bought at the Starter rate. Free tier is $0 until it stops. */
export function monthlyCostUsd(input: {
  sites: number;
  callsPerSitePerDay: number;
  days?: number;
  usdPerCredit?: number;
  llmUsd?: number;
}): { credits: number; usd: number } {
  const days = input.days ?? 30;
  const usdPerCredit = input.usdPerCredit ?? 0.005;
  const credits = input.sites * input.callsPerSitePerDay * days;
  const usd = credits * usdPerCredit + (input.llmUsd ?? 0);
  return { credits, usd };
}

export const DAILY_DATA_CALLS_PER_SITE = 3;
