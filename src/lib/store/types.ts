import type { DailySummary } from "../rules/types";

export type SubscriptionStatus = "lead" | "connected" | "pending_payment" | "active" | "cancelled";

export type Customer = {
  id: string;
  name: string;
  email: string;
  phone: string;
  telegramChatId: string | null;
  telegramLinkToken: string;
  subscriptionStatus: SubscriptionStatus;
  createdAt: string;
};

export type SiteRecord = {
  id: string;
  customerId: string;
  solaredgeSiteId: string;
  portalName: string;
  cardName: string;
  reportLabel: string | null;
  timezone: string;
  latitude: number | null;
  longitude: number | null;
  installedKwDc: number | null;
  portalPeakKw: number | null;
  tariffAgorotPerKwh: number | null;
  incomeBasis: "all_production" | "export_only";
  expectedDailyWh: number | null;
  soilingIndex: number;
  monitor: boolean;
  createdAt: string;
};

export type TokenRecord = {
  siteId: string;
  accessToken: string;
  refreshToken: string;
  accessExpiresAt: string | null;
  updatedAt: string;
  lastRefreshError: string | null;
  lastRefreshOkAt: string | null;
};

export type TokenMeta = {
  siteId: string;
  hasToken: boolean;
  updatedAt: string | null;
  accessExpiresAt: string | null;
  lastRefreshError: string | null;
  lastRefreshOkAt: string | null;
};

export type PullKind = "daily" | "early";

export type PullRecord = {
  id: string;
  siteId: string;
  customerId: string;
  pulledAt: string;
  localDate: string;
  kind: PullKind;
  endpoint: string;
  credits: number;
  ok: boolean;
  body: unknown;
};

export type SummaryRecord = {
  id: string;
  customerId: string;
  localDate: string;
  payload: DailySummary;
  text: string;
  createdAt: string;
  sentAt: string | null;
};

export type AlertRecord = {
  id: string;
  siteId: string;
  customerId: string;
  dedupeKey: string;
  code: string;
  serial: string | null;
  openedAt: string;
  status: string;
  detectedAt: string;
  notifiedAt: string | null;
};

export type PaymentRecord = {
  id: string;
  customerId: string;
  amountAgorot: number;
  currency: "ILS";
  status: "stub_pending";
  provider: string;
  description: string;
  createdAt: string;
};

export type AdminCustomerRow = {
  customer: Customer;
  sites: SiteRecord[];
  lastPullAt: string | null;
  creditsThisMonth: number;
  llmTokensThisMonth: number;
  failedRefreshes: TokenMeta[];
};

export interface Store {
  createCustomer(input: { name: string; email: string; phone: string }): Promise<Customer>;
  getCustomer(id: string): Promise<Customer | null>;
  getCustomerByLinkToken(token: string): Promise<Customer | null>;
  listCustomers(): Promise<Customer[]>;
  setTelegramChat(customerId: string, chatId: string): Promise<void>;
  setSubscription(customerId: string, status: SubscriptionStatus): Promise<void>;

  createOauthSession(customerId: string): Promise<string>;
  consumeOauthSession(sessionId: string): Promise<string | null>;

  upsertSite(site: Omit<SiteRecord, "createdAt"> & { createdAt?: string }): Promise<SiteRecord>;
  listSites(customerId: string): Promise<SiteRecord[]>;
  listMonitoredSites(): Promise<SiteRecord[]>;
  setMonitor(customerId: string, siteIds: string[], monitor: boolean): Promise<void>;

  saveToken(record: TokenRecord): Promise<void>;
  readToken(siteId: string): Promise<TokenRecord | null>;
  recordRefreshFailure(siteId: string, error: string): Promise<void>;
  tokenMeta(siteId: string): Promise<TokenMeta>;

  insertPull(pull: Omit<PullRecord, "id"> & { id?: string }): Promise<PullRecord>;
  hasPull(siteId: string, localDate: string, kind: PullKind, endpoint: string): Promise<boolean>;
  listPulls(siteId: string): Promise<PullRecord[]>;

  addCredits(input: { customerId: string; siteId: string; credits: number; endpoint: string; localDate: string; at: string }): Promise<void>;
  creditsSince(customerId: string, sinceIso: string): Promise<number>;

  saveSummary(record: Omit<SummaryRecord, "id" | "createdAt"> & { id?: string; createdAt?: string }): Promise<SummaryRecord>;
  getSummary(customerId: string, localDate: string): Promise<SummaryRecord | null>;
  latestSummary(customerId: string): Promise<SummaryRecord | null>;
  markSummarySent(customerId: string, localDate: string, sentAt: string): Promise<void>;

  upsertAlert(alert: Omit<AlertRecord, "id"> & { id?: string }): Promise<AlertRecord>;
  unnotifiedAlerts(customerId: string): Promise<AlertRecord[]>;
  markAlertNotified(id: string, notifiedAt: string): Promise<void>;

  addLlmUsage(input: { customerId: string; model: string; inputTokens: number; outputTokens: number; at: string }): Promise<void>;
  llmTokensSince(customerId: string, sinceIso: string): Promise<number>;

  createPayment(input: Omit<PaymentRecord, "id" | "createdAt" | "status" | "currency"> & { id?: string }): Promise<PaymentRecord>;
  getPayment(id: string): Promise<PaymentRecord | null>;

  adminRows(): Promise<AdminCustomerRow[]>;
}
