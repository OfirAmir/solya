import { incomeAgorot } from "./income";
import {
  composeRay,
  environmentScore,
  healthScore,
  performanceScore,
  rayBand,
  weightedRay,
} from "./ray";
import type { DailySummary, SiteDayInput, SiteDayResult } from "./types";

export type RulesOptions = {
  includeWeather: boolean;
};

export function evaluateSite(input: SiteDayInput, options: RulesOptions): SiteDayResult {
  const missing = input.productionWh == null;
  const communicationLost = input.communicationLost || missing;
  const measuredZero = !communicationLost && input.productionWh === 0;
  const openAlerts = input.openAlerts.filter((alert) => alert.status === "OPEN");

  if (communicationLost) {
    return {
      siteId: input.siteId,
      portalName: input.portalName,
      cardName: input.cardName,
      reportLabel: input.reportLabel,
      installedKwDc: input.installedKwDc,
      productionWh: null,
      exportWh: null,
      incomeAgorot: null,
      ray: null,
      rayBand: null,
      performance: null,
      health: null,
      environment: null,
      communicationLost: true,
      measuredZero: false,
      openAlerts,
    };
  }

  const performance = performanceScore(input.productionWh, input.expectedWh);
  const health = healthScore({
    communicationLost: false,
    measuredZero,
    openAlertCount: openAlerts.length,
  });
  const environment = environmentScore({
    includeWeather: options.includeWeather,
    cloudCoverPercent: input.cloudCoverPercent,
    soilingIndex: input.soilingIndex,
  });
  const ray =
    performance != null && health != null ? composeRay(performance, health, environment) : null;

  const income = measuredZero
    ? null
    : incomeAgorot({
        productionWh: input.productionWh,
        exportWh: input.exportWh,
        tariffAgorotPerKwh: input.tariffAgorotPerKwh,
        basis: input.incomeBasis,
      });

  return {
    siteId: input.siteId,
    portalName: input.portalName,
    cardName: input.cardName,
    reportLabel: input.reportLabel,
    installedKwDc: input.installedKwDc,
    productionWh: measuredZero ? null : input.productionWh,
    exportWh: input.exportWh,
    incomeAgorot: income,
    ray,
    rayBand: ray == null ? null : rayBand(ray),
    performance,
    health,
    environment,
    communicationLost: false,
    measuredZero,
    openAlerts,
  };
}

export function buildDailySummary(inputs: SiteDayInput[], options: RulesOptions): DailySummary {
  const sites = inputs.map((input) => evaluateSite(input, options));
  const reportable = sites.filter((site) => !site.communicationLost && !site.measuredZero);
  const complete =
    sites.length > 0 && sites.every((site) => !site.communicationLost && !site.measuredZero);
  const totalProductionWh = complete
    ? reportable.reduce((sum, site) => sum + (site.productionWh ?? 0), 0)
    : null;
  const incomes = reportable.map((site) => site.incomeAgorot);
  const totalIncomeAgorot =
    complete && incomes.every((value) => value != null)
      ? incomes.reduce((sum, value) => (sum ?? 0) + (value ?? 0), 0)
      : null;
  const overallRay = weightedRay(sites);
  return {
    sites,
    totalProductionWh,
    totalIncomeAgorot,
    overallRay,
    overallBand: overallRay == null ? null : rayBand(overallRay),
    anyCommunicationLost: sites.some((site) => site.communicationLost),
    anyMeasuredZero: sites.some((site) => site.measuredZero),
    openAlertCount: sites.reduce((sum, site) => sum + site.openAlerts.length, 0),
  };
}
