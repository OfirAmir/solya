import { decryptString, encryptString, randomToken } from "../crypto";
import type {
  AdminCustomerRow,
  AlertRecord,
  Customer,
  PaymentRecord,
  PullKind,
  PullRecord,
  SiteRecord,
  Store,
  SubscriptionStatus,
  SummaryRecord,
  TokenMeta,
  TokenRecord,
} from "./types";

type Cipher = { access: string; refresh: string; meta: Omit<TokenRecord, "accessToken" | "refreshToken"> };

export class MemoryStore implements Store {
  readonly customers = new Map<string, Customer>();
  readonly sessions = new Map<string, { customerId: string; consumedAt: string | null }>();
  readonly sites = new Map<string, SiteRecord>();
  readonly pulls: PullRecord[] = [];
  readonly summaries = new Map<string, SummaryRecord>();
  readonly alerts = new Map<string, AlertRecord>();
  readonly credits: { customerId: string; siteId: string; credits: number; endpoint: string; localDate: string; at: string }[] = [];
  readonly llm: { customerId: string; model: string; inputTokens: number; outputTokens: number; at: string }[] = [];
  readonly payments = new Map<string, PaymentRecord>();
  private readonly ciphers = new Map<string, Cipher>();

  constructor(private readonly key: Buffer) {}

  /** Test hook. Ciphertext must not contain the refresh token. */
  debugCipher(siteId: string): { access: string; refresh: string } | null {
    const row = this.ciphers.get(siteId);
    return row ? { access: row.access, refresh: row.refresh } : null;
  }

  async createCustomer(input: { name: string; email: string; phone: string }): Promise<Customer> {
    const customer: Customer = {
      id: crypto.randomUUID(),
      name: input.name.trim(),
      email: input.email.trim(),
      phone: input.phone.trim(),
      telegramChatId: null,
      telegramLinkToken: randomToken(18),
      subscriptionStatus: "lead",
      createdAt: new Date().toISOString(),
    };
    this.customers.set(customer.id, customer);
    return customer;
  }

  async getCustomer(id: string): Promise<Customer | null> {
    return this.customers.get(id) ?? null;
  }

  async getCustomerByLinkToken(token: string): Promise<Customer | null> {
    for (const customer of this.customers.values()) {
      if (customer.telegramLinkToken === token) return customer;
    }
    return null;
  }

  async listCustomers(): Promise<Customer[]> {
    return [...this.customers.values()];
  }

  async setTelegramChat(customerId: string, chatId: string): Promise<void> {
    const customer = this.mustCustomer(customerId);
    customer.telegramChatId = chatId;
    if (customer.subscriptionStatus === "lead") customer.subscriptionStatus = "connected";
  }

  async setSubscription(customerId: string, status: SubscriptionStatus): Promise<void> {
    this.mustCustomer(customerId).subscriptionStatus = status;
  }

  async createOauthSession(customerId: string): Promise<string> {
    const id = randomToken(18);
    this.sessions.set(id, { customerId, consumedAt: null });
    return id;
  }

  async consumeOauthSession(sessionId: string): Promise<string | null> {
    const session = this.sessions.get(sessionId);
    if (!session || session.consumedAt) return null;
    session.consumedAt = new Date().toISOString();
    return session.customerId;
  }

  async upsertSite(site: Omit<SiteRecord, "createdAt"> & { createdAt?: string }): Promise<SiteRecord> {
    const existing = [...this.sites.values()].find(
      (row) => row.customerId === site.customerId && row.solaredgeSiteId === site.solaredgeSiteId,
    );
    const record: SiteRecord = {
      ...site,
      id: existing?.id ?? site.id,
      createdAt: existing?.createdAt ?? site.createdAt ?? new Date().toISOString(),
    };
    this.sites.set(record.id, record);
    return record;
  }

  async listSites(customerId: string): Promise<SiteRecord[]> {
    return [...this.sites.values()].filter((site) => site.customerId === customerId);
  }

  async listMonitoredSites(): Promise<SiteRecord[]> {
    return [...this.sites.values()].filter((site) => site.monitor);
  }

  async setMonitor(customerId: string, siteIds: string[], monitor: boolean): Promise<void> {
    for (const site of this.sites.values()) {
      if (site.customerId !== customerId) continue;
      if (siteIds.includes(site.id)) site.monitor = monitor;
    }
  }

  async saveToken(record: TokenRecord): Promise<void> {
    this.ciphers.set(record.siteId, {
      access: encryptString(record.accessToken, this.key),
      refresh: encryptString(record.refreshToken, this.key),
      meta: {
        siteId: record.siteId,
        accessExpiresAt: record.accessExpiresAt,
        updatedAt: record.updatedAt,
        lastRefreshError: null,
        lastRefreshOkAt: record.lastRefreshOkAt,
      },
    });
  }

  async readToken(siteId: string): Promise<TokenRecord | null> {
    const row = this.ciphers.get(siteId);
    if (!row) return null;
    return {
      ...row.meta,
      accessToken: decryptString(row.access, this.key),
      refreshToken: decryptString(row.refresh, this.key),
    };
  }

  async recordRefreshFailure(siteId: string, error: string): Promise<void> {
    const row = this.ciphers.get(siteId);
    if (!row) return;
    row.meta.lastRefreshError = error;
    row.meta.updatedAt = new Date().toISOString();
  }

  async tokenMeta(siteId: string): Promise<TokenMeta> {
    const row = this.ciphers.get(siteId);
    if (!row) {
      return {
        siteId,
        hasToken: false,
        updatedAt: null,
        accessExpiresAt: null,
        lastRefreshError: null,
        lastRefreshOkAt: null,
      };
    }
    return {
      siteId,
      hasToken: true,
      updatedAt: row.meta.updatedAt,
      accessExpiresAt: row.meta.accessExpiresAt,
      lastRefreshError: row.meta.lastRefreshError,
      lastRefreshOkAt: row.meta.lastRefreshOkAt,
    };
  }

  async insertPull(pull: Omit<PullRecord, "id"> & { id?: string }): Promise<PullRecord> {
    const record: PullRecord = { ...pull, id: pull.id ?? crypto.randomUUID() };
    this.pulls.push(record);
    return record;
  }

  async hasPull(siteId: string, localDate: string, kind: PullKind, endpoint: string): Promise<boolean> {
    return this.pulls.some(
      (pull) =>
        pull.siteId === siteId &&
        pull.localDate === localDate &&
        pull.kind === kind &&
        pull.endpoint === endpoint &&
        pull.ok,
    );
  }

  async listPulls(siteId: string): Promise<PullRecord[]> {
    return this.pulls.filter((pull) => pull.siteId === siteId);
  }

  async addCredits(input: { customerId: string; siteId: string; credits: number; endpoint: string; localDate: string; at: string }): Promise<void> {
    this.credits.push(input);
  }

  async creditsSince(customerId: string, sinceIso: string): Promise<number> {
    return this.credits
      .filter((row) => row.customerId === customerId && row.at >= sinceIso)
      .reduce((sum, row) => sum + row.credits, 0);
  }

  async saveSummary(record: Omit<SummaryRecord, "id" | "createdAt"> & { id?: string; createdAt?: string }): Promise<SummaryRecord> {
    const key = `${record.customerId}:${record.localDate}`;
    const existing = this.summaries.get(key);
    const saved: SummaryRecord = {
      id: existing?.id ?? record.id ?? crypto.randomUUID(),
      customerId: record.customerId,
      localDate: record.localDate,
      payload: record.payload,
      text: record.text,
      createdAt: existing?.createdAt ?? record.createdAt ?? new Date().toISOString(),
      sentAt: record.sentAt,
    };
    this.summaries.set(key, saved);
    return saved;
  }

  async getSummary(customerId: string, localDate: string): Promise<SummaryRecord | null> {
    return this.summaries.get(`${customerId}:${localDate}`) ?? null;
  }

  async latestSummary(customerId: string): Promise<SummaryRecord | null> {
    const rows = [...this.summaries.values()]
      .filter((row) => row.customerId === customerId)
      .sort((a, b) => a.localDate.localeCompare(b.localDate));
    return rows.at(-1) ?? null;
  }

  async markSummarySent(customerId: string, localDate: string, sentAt: string): Promise<void> {
    const row = this.summaries.get(`${customerId}:${localDate}`);
    if (row) row.sentAt = sentAt;
  }

  async upsertAlert(alert: Omit<AlertRecord, "id"> & { id?: string }): Promise<AlertRecord> {
    const existing = [...this.alerts.values()].find(
      (row) => row.siteId === alert.siteId && row.dedupeKey === alert.dedupeKey,
    );
    const record: AlertRecord = {
      ...alert,
      id: existing?.id ?? alert.id ?? crypto.randomUUID(),
      notifiedAt: existing?.notifiedAt ?? alert.notifiedAt,
    };
    this.alerts.set(record.id, record);
    return record;
  }

  async unnotifiedAlerts(customerId: string): Promise<AlertRecord[]> {
    return [...this.alerts.values()].filter((row) => row.customerId === customerId && !row.notifiedAt && row.status === "OPEN");
  }

  async markAlertNotified(id: string, notifiedAt: string): Promise<void> {
    const row = this.alerts.get(id);
    if (row) row.notifiedAt = notifiedAt;
  }

  async addLlmUsage(input: { customerId: string; model: string; inputTokens: number; outputTokens: number; at: string }): Promise<void> {
    this.llm.push(input);
  }

  async llmTokensSince(customerId: string, sinceIso: string): Promise<number> {
    return this.llm
      .filter((row) => row.customerId === customerId && row.at >= sinceIso)
      .reduce((sum, row) => sum + row.inputTokens + row.outputTokens, 0);
  }

  async createPayment(input: Omit<PaymentRecord, "id" | "createdAt" | "status" | "currency"> & { id?: string }): Promise<PaymentRecord> {
    const record: PaymentRecord = {
      id: input.id ?? crypto.randomUUID(),
      customerId: input.customerId,
      amountAgorot: input.amountAgorot,
      currency: "ILS",
      status: "stub_pending",
      provider: input.provider,
      description: input.description,
      createdAt: new Date().toISOString(),
    };
    this.payments.set(record.id, record);
    return record;
  }

  async getPayment(id: string): Promise<PaymentRecord | null> {
    return this.payments.get(id) ?? null;
  }

  async adminRows(): Promise<AdminCustomerRow[]> {
    const month = new Date();
    month.setUTCDate(1);
    month.setUTCHours(0, 0, 0, 0);
    const since = month.toISOString();
    const rows: AdminCustomerRow[] = [];
    for (const customer of this.customers.values()) {
      const sites = await this.listSites(customer.id);
      const pulls = this.pulls.filter((pull) => pull.customerId === customer.id);
      const lastPullAt = pulls.map((pull) => pull.pulledAt).sort().at(-1) ?? null;
      rows.push({
        customer,
        sites,
        lastPullAt,
        creditsThisMonth: await this.creditsSince(customer.id, since),
        llmTokensThisMonth: await this.llmTokensSince(customer.id, since),
        failedRefreshes: await Promise.all(
          sites.map((site) => this.tokenMeta(site.id)),
        ),
      });
    }
    return rows;
  }

  private mustCustomer(id: string): Customer {
    const customer = this.customers.get(id);
    if (!customer) throw new Error("Unknown customer.");
    return customer;
  }
}
