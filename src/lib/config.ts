/**
 * Open decisions from handover section 5.5 stay in the environment.
 * Defaults are the safe or already-closed choices, not guesses for the open ones.
 */

export type VatMode = "unset" | "included" | "excluded";
export type SolarEdgeMode = "live" | "fixture";

export type AppConfig = {
  publicAppUrl: string;
  appSecret: string;
  solaredge: {
    mode: SolarEdgeMode;
    clientId: string;
    clientSecret: string;
    redirectUri: string;
    authorizeUrl: string;
    tokenUrl: string;
    apiBase: string;
    scopes: string;
    accessDuration: string;
    refreshIdleDays: number;
  };
  polling: {
    dailyEnabled: boolean;
    earlyPullEnabled: boolean;
    earlyPullMaxCreditsPerSite: number;
    maxCreditsPerCustomerMonth: number;
    sendWindowHours: number;
    fallbackSendHour: number;
  };
  domain: {
    canonical: string;
    marketing: string;
  };
  solMayOpenServiceCalls: boolean;
  ray: {
    includeWeather: boolean;
    weatherSource: "none" | "open-meteo";
  };
  vat: {
    mode: VatMode;
    /** Null until a rate is configured. 17% is not assumed. */
    rate: number | null;
  };
  payments: {
    provider: string;
  };
  llm: {
    enabled: boolean;
    apiKey: string;
    model: string;
    baseUrl: string;
    monthlyTokenBudget: number;
  };
  telegram: {
    botToken: string;
    botUsername: string;
    webhookSecret: string;
  };
  admin: {
    password: string;
    sessionSecret: string;
  };
  cronSecret: string;
  tokenEncryptionKey: string;
  databaseUrl: string;
  usdPerSolarEdgeCredit: number;
};

function bool(value: string | undefined, fallback: boolean): boolean {
  if (value == null || value.trim() === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

function num(value: string | undefined, fallback: number): number {
  if (value == null || value.trim() === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function str(value: string | undefined, fallback = ""): string {
  return value == null ? fallback : value.trim();
}

export function loadConfig(env: Record<string, string | undefined> = process.env): AppConfig {
  const vatModeRaw = str(env.SOLYA_VAT_MODE, "unset");
  const vatMode: VatMode =
    vatModeRaw === "included" || vatModeRaw === "excluded" ? vatModeRaw : "unset";
  const vatRateRaw = str(env.SOLYA_VAT_RATE);
  const vatRate = vatRateRaw === "" ? null : Number(vatRateRaw);
  const weather = str(env.SOLYA_WEATHER_SOURCE, "none");
  const mode = str(env.SOLAREDGE_CLIENT, "fixture") === "live" ? "live" : "fixture";

  return {
    publicAppUrl: str(env.APP_URL).replace(/\/$/, ""),
    appSecret: str(env.APP_SECRET),
    solaredge: {
      mode,
      clientId: str(env.SOLAREDGE_CLIENT_ID),
      clientSecret: str(env.SOLAREDGE_CLIENT_SECRET),
      redirectUri: str(env.SOLAREDGE_REDIRECT_URI),
      authorizeUrl: str(env.SOLAREDGE_AUTHORIZE_URL, "https://connect.solaredge.com/authorize"),
      tokenUrl: str(env.SOLAREDGE_TOKEN_URL, "https://monitoringapi.solaredge.com/v2/oauth2/token"),
      apiBase: str(env.SOLAREDGE_API_BASE, "https://monitoringapi.solaredge.com/v2").replace(
        /\/$/,
        "",
      ),
      scopes: str(env.SOLAREDGE_SCOPES, "SITE_DATA DEVICE_DATA"),
      accessDuration: str(env.SOLAREDGE_ACCESS_DURATION, "P24M"),
      refreshIdleDays: num(env.SOLYA_REFRESH_IDLE_DAYS, 14),
    },
    polling: {
      dailyEnabled: bool(env.SOLYA_DAILY_PULL, true),
      earlyPullEnabled: bool(env.SOLYA_EARLY_PULL, false),
      earlyPullMaxCreditsPerSite: num(env.SOLYA_EARLY_PULL_MAX_CREDITS_PER_SITE, 1),
      maxCreditsPerCustomerMonth: num(env.SOLYA_MAX_CREDITS_PER_CUSTOMER_MONTH, 400),
      sendWindowHours: num(env.SOLYA_SEND_WINDOW_HOURS, 6),
      fallbackSendHour: num(env.SOLYA_FALLBACK_SEND_HOUR, 17),
    },
    domain: {
      canonical: str(env.SOLYA_CANONICAL_DOMAIN),
      marketing: str(env.SOLYA_MARKETING_DOMAIN),
    },
    solMayOpenServiceCalls: bool(env.SOLYA_MAY_OPEN_SERVICE_CALLS, false),
    ray: {
      includeWeather: bool(env.SOLYA_RAY_INCLUDE_WEATHER, true),
      weatherSource: weather === "open-meteo" ? "open-meteo" : "none",
    },
    vat: {
      mode: vatMode,
      rate: vatRate != null && Number.isFinite(vatRate) ? vatRate : null,
    },
    payments: {
      provider: str(env.SOLYA_PAYMENT_PROVIDER, "stub") || "stub",
    },
    llm: {
      enabled: bool(env.SOLYA_LLM_ENABLED, false),
      apiKey: str(env.OPENROUTER_API_KEY),
      model: str(env.OPENROUTER_MODEL),
      baseUrl: str(env.OPENROUTER_BASE_URL, "https://openrouter.ai/api/v1").replace(/\/$/, ""),
      monthlyTokenBudget: num(env.SOLYA_LLM_MONTHLY_TOKEN_BUDGET, 20000),
    },
    telegram: {
      botToken: str(env.TELEGRAM_BOT_TOKEN),
      botUsername: str(env.TELEGRAM_BOT_USERNAME).replace(/^@/, ""),
      webhookSecret: str(env.TELEGRAM_WEBHOOK_SECRET),
    },
    admin: {
      password: str(env.ADMIN_PASSWORD),
      sessionSecret: str(env.ADMIN_SESSION_SECRET),
    },
    cronSecret: str(env.CRON_SECRET),
    tokenEncryptionKey: str(env.TOKEN_ENCRYPTION_KEY),
    databaseUrl: str(env.DATABASE_URL),
    usdPerSolarEdgeCredit: num(env.SOLYA_USD_PER_CREDIT, 0.005),
  };
}
