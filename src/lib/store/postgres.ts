import { Pool } from "pg";
import { decryptString, encryptString, randomToken } from "../crypto";
import type { DailySummary } from "../rules/types";
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

let pool: Pool | null = null;

export function getPool(databaseUrl: string): Pool {
  if (!pool) {
    const local = /localhost|127\.0\.0\.1/.test(databaseUrl);
    pool = new Pool({
      connectionString: databaseUrl,
      max: 3,
      ssl: local ? undefined : { rejectUnauthorized: false },
    });
  }
  return pool;
}

function customer(row: Record<string, unknown>): Customer {
  return {
    id: String(row.id),
    name: String(row.name),
    email: String(row.email),
    phone: String(row.phone),
    telegramChatId: row.telegram_chat_id == null ? null : String(row.telegram_chat_id),
    telegramLinkToken: String(row.telegram_link_token),
    subscriptionStatus: String(row.subscription_status) as SubscriptionStatus,
    createdAt: new Date(String(row.created_at)).toISOString(),
  };
}

function site(row: Record<string, unknown>): SiteRecord {
  return {
    id: String(row.id),
    customerId: String(row.customer_id),
    solaredgeSiteId: String(row.solaredge_site_id),
    portalName: String(row.portal_name),
    cardName: String(row.card_name),
    reportLabel: row.report_label == null ? null : String(row.report_label),
    timezone: String(row.timezone),
    latitude: row.latitude == null ? null : Number(row.latitude),
    longitude: row.longitude == null ? null : Number(row.longitude),
    installedKwDc: row.installed_kw_dc == null ? null : Number(row.installed_kw_dc),
    portalPeakKw: row.portal_peak_kw == null ? null : Number(row.portal_peak_kw),
    tariffAgorotPerKwh: row.tariff_agorot_per_kwh == null ? null : Number(row.tariff_agorot_per_kwh),
    incomeBasis: String(row.income_basis) as SiteRecord["incomeBasis"],
    expectedDailyWh: row.expected_daily_wh == null ? null : Number(row.expected_daily_wh),
    soilingIndex: Number(row.soiling_index ?? 0),
    monitor: Boolean(row.monitor),
    createdAt: new Date(String(row.created_at)).toISOString(),
  };
}

export class PostgresStore implements Store {
  constructor(
    private readonly db: Pool,
    private readonly key: Buffer,
  ) {}

  async createCustomer(input: { name: string; email: string; phone: string }): Promise<Customer> {
    const id = crypto.randomUUID();
    const token = randomToken(18);
    const result = await this.db.query(
      `insert into customers (id, name, email, phone, telegram_link_token, subscription_status)
       values ($1, $2, $3, $4, $5, 'lead') returning *`,
      [id, input.name.trim(), input.email.trim(), input.phone.trim(), token],
    );
    return customer(result.rows[0]);
  }

  async getCustomer(id: string): Promise<Customer | null> {
    const result = await this.db.query("select * from customers where id = $1", [id]);
    return result.rows[0] ? customer(result.rows[0]) : null;
  }

  async getCustomerByLinkToken(token: string): Promise<Customer | null> {
    const result = await this.db.query("select * from customers where telegram_link_token = $1", [token]);
    return result.rows[0] ? customer(result.rows[0]) : null;
  }

  async listCustomers(): Promise<Customer[]> {
    const result = await this.db.query("select * from customers order by created_at");
    return result.rows.map(customer);
  }

  async setTelegramChat(customerId: string, chatId: string): Promise<void> {
    await this.db.query(
      `update customers
         set telegram_chat_id = $2,
             subscription_status = case when subscription_status = 'lead' then 'connected' else subscription_status end
       where id = $1`,
      [customerId, chatId],
    );
  }

  async setSubscription(customerId: string, status: SubscriptionStatus): Promise<void> {
    await this.db.query("update customers set subscription_status = $2 where id = $1", [customerId, status]);
  }

  async createOauthSession(customerId: string): Promise<string> {
    const id = randomToken(18);
    await this.db.query("insert into oauth_sessions (id, customer_id) values ($1, $2)", [id, customerId]);
    return id;
  }

  async consumeOauthSession(sessionId: string): Promise<string | null> {
    const result = await this.db.query(
      `update oauth_sessions set consumed_at = now()
        where id = $1 and consumed_at is null
        returning customer_id`,
      [sessionId],
    );
    return result.rows[0] ? String(result.rows[0].customer_id) : null;
  }

  async upsertSite(input: Omit<SiteRecord, "createdAt"> & { createdAt?: string }): Promise<SiteRecord> {
    const result = await this.db.query(
      `insert into sites (
         id, customer_id, solaredge_site_id, portal_name, card_name, report_label, timezone,
         latitude, longitude, installed_kw_dc, portal_peak_kw, tariff_agorot_per_kwh,
         income_basis, expected_daily_wh, soiling_index, monitor
       ) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
       on conflict (customer_id, solaredge_site_id) do update set
         portal_name = excluded.portal_name,
         card_name = excluded.card_name,
         portal_peak_kw = excluded.portal_peak_kw,
         latitude = coalesce(excluded.latitude, sites.latitude),
         longitude = coalesce(excluded.longitude, sites.longitude),
         timezone = excluded.timezone
       returning *`,
      [
        input.id,
        input.customerId,
        input.solaredgeSiteId,
        input.portalName,
        input.cardName,
        input.reportLabel,
        input.timezone,
        input.latitude,
        input.longitude,
        input.installedKwDc,
        input.portalPeakKw,
        input.tariffAgorotPerKwh,
        input.incomeBasis,
        input.expectedDailyWh,
        input.soilingIndex,
        input.monitor,
      ],
    );
    return site(result.rows[0]);
  }

  async listSites(customerId: string): Promise<SiteRecord[]> {
    const result = await this.db.query("select * from sites where customer_id = $1 order by created_at", [customerId]);
    return result.rows.map(site);
  }

  async listMonitoredSites(): Promise<SiteRecord[]> {
    const result = await this.db.query("select * from sites where monitor = true order by created_at");
    return result.rows.map(site);
  }

  async setMonitor(customerId: string, siteIds: string[], monitor: boolean): Promise<void> {
    if (siteIds.length === 0) return;
    await this.db.query(
      "update sites set monitor = $3 where customer_id = $1 and id = any($2::text[])",
      [customerId, siteIds, monitor],
    );
  }

  async saveToken(record: TokenRecord): Promise<void> {
    await this.db.query(
      `insert into site_tokens (
         site_id, access_ciphertext, refresh_ciphertext, access_expires_at, updated_at, last_refresh_error, last_refresh_ok_at
       ) values ($1, $2, $3, $4, $5, null, $6)
       on conflict (site_id) do update set
         access_ciphertext = excluded.access_ciphertext,
         refresh_ciphertext = excluded.refresh_ciphertext,
         access_expires_at = excluded.access_expires_at,
         updated_at = excluded.updated_at,
         last_refresh_error = null,
         last_refresh_ok_at = excluded.last_refresh_ok_at`,
      [
        record.siteId,
        encryptString(record.accessToken, this.key),
        encryptString(record.refreshToken, this.key),
        record.accessExpiresAt,
        record.updatedAt,
        record.lastRefreshOkAt,
      ],
    );
  }

  async readToken(siteId: string): Promise<TokenRecord | null> {
    const result = await this.db.query("select * from site_tokens where site_id = $1", [siteId]);
    const row = result.rows[0];
    if (!row) return null;
    return {
      siteId,
      accessToken: decryptString(String(row.access_ciphertext), this.key),
      refreshToken: decryptString(String(row.refresh_ciphertext), this.key),
      accessExpiresAt: row.access_expires_at ? new Date(String(row.access_expires_at)).toISOString() : null,
      updatedAt: new Date(String(row.updated_at)).toISOString(),
      lastRefreshError: row.last_refresh_error == null ? null : String(row.last_refresh_error),
      lastRefreshOkAt: row.last_refresh_ok_at ? new Date(String(row.last_refresh_ok_at)).toISOString() : null,
    };
  }

  async recordRefreshFailure(siteId: string, error: string): Promise<void> {
    await this.db.query(
      "update site_tokens set last_refresh_error = $2, updated_at = now() where site_id = $1",
      [siteId, error],
    );
  }

  async tokenMeta(siteId: string): Promise<TokenMeta> {
    const result = await this.db.query(
      "select updated_at, access_expires_at, last_refresh_error, last_refresh_ok_at from site_tokens where site_id = $1",
      [siteId],
    );
    const row = result.rows[0];
    if (!row) {
      return { siteId, hasToken: false, updatedAt: null, accessExpiresAt: null, lastRefreshError: null, lastRefreshOkAt: null };
    }
    return {
      siteId,
      hasToken: true,
      updatedAt: new Date(String(row.updated_at)).toISOString(),
      accessExpiresAt: row.access_expires_at ? new Date(String(row.access_expires_at)).toISOString() : null,
      lastRefreshError: row.last_refresh_error == null ? null : String(row.last_refresh_error),
      lastRefreshOkAt: row.last_refresh_ok_at ? new Date(String(row.last_refresh_ok_at)).toISOString() : null,
    };
  }

  async insertPull(pull: Omit<PullRecord, "id"> & { id?: string }): Promise<PullRecord> {
    const id = pull.id ?? crypto.randomUUID();
    await this.db.query(
      `insert into raw_pulls (id, site_id, customer_id, pulled_at, local_date, kind, endpoint, credits, ok, body)
       values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
      [id, pull.siteId, pull.customerId, pull.pulledAt, pull.localDate, pull.kind, pull.endpoint, pull.credits, pull.ok, JSON.stringify(pull.body)],
    );
    return { ...pull, id };
  }

  async hasPull(siteId: string, localDate: string, kind: PullKind, endpoint: string): Promise<boolean> {
    const result = await this.db.query(
      `select 1 from raw_pulls
        where site_id = $1 and local_date = $2 and kind = $3 and endpoint = $4 and ok = true
        limit 1`,
      [siteId, localDate, kind, endpoint],
    );
    return result.rowCount != null && result.rowCount > 0;
  }

  async listPulls(siteId: string): Promise<PullRecord[]> {
    const result = await this.db.query("select * from raw_pulls where site_id = $1 order by pulled_at", [siteId]);
    return result.rows.map((row) => ({
      id: String(row.id),
      siteId: String(row.site_id),
      customerId: String(row.customer_id),
      pulledAt: new Date(String(row.pulled_at)).toISOString(),
      localDate: String(row.local_date).slice(0, 10),
      kind: String(row.kind) as PullKind,
      endpoint: String(row.endpoint),
      credits: Number(row.credits),
      ok: Boolean(row.ok),
      body: row.body,
    }));
  }

  async addCredits(input: { customerId: string; siteId: string; credits: number; endpoint: string; localDate: string; at: string }): Promise<void> {
    await this.db.query(
      `insert into credit_ledger (id, customer_id, site_id, at, credits, endpoint, local_date)
       values ($1,$2,$3,$4,$5,$6,$7)`,
      [crypto.randomUUID(), input.customerId, input.siteId, input.at, input.credits, input.endpoint, input.localDate],
    );
  }

  async creditsSince(customerId: string, sinceIso: string): Promise<number> {
    const result = await this.db.query(
      "select coalesce(sum(credits), 0)::int as credits from credit_ledger where customer_id = $1 and at >= $2",
      [customerId, sinceIso],
    );
    return Number(result.rows[0]?.credits ?? 0);
  }

  async saveSummary(record: Omit<SummaryRecord, "id" | "createdAt"> & { id?: string; createdAt?: string }): Promise<SummaryRecord> {
    const id = record.id ?? crypto.randomUUID();
    const result = await this.db.query(
      `insert into daily_summaries (id, customer_id, local_date, payload, text, sent_at)
       values ($1,$2,$3,$4,$5,$6)
       on conflict (customer_id, local_date) do update set
         payload = excluded.payload,
         text = excluded.text,
         sent_at = coalesce(daily_summaries.sent_at, excluded.sent_at)
       returning *`,
      [id, record.customerId, record.localDate, JSON.stringify(record.payload), record.text, record.sentAt],
    );
    return summary(result.rows[0]);
  }

  async getSummary(customerId: string, localDate: string): Promise<SummaryRecord | null> {
    const result = await this.db.query(
      "select * from daily_summaries where customer_id = $1 and local_date = $2",
      [customerId, localDate],
    );
    return result.rows[0] ? summary(result.rows[0]) : null;
  }

  async latestSummary(customerId: string): Promise<SummaryRecord | null> {
    const result = await this.db.query(
      "select * from daily_summaries where customer_id = $1 order by local_date desc limit 1",
      [customerId],
    );
    return result.rows[0] ? summary(result.rows[0]) : null;
  }

  async markSummarySent(customerId: string, localDate: string, sentAt: string): Promise<void> {
    await this.db.query(
      "update daily_summaries set sent_at = $3 where customer_id = $1 and local_date = $2",
      [customerId, localDate, sentAt],
    );
  }

  async upsertAlert(alert: Omit<AlertRecord, "id"> & { id?: string }): Promise<AlertRecord> {
    const result = await this.db.query(
      `insert into alerts (id, site_id, customer_id, dedupe_key, code, serial, opened_at, status, detected_at, notified_at, raw)
       values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
       on conflict (site_id, dedupe_key) do update set status = excluded.status
       returning *`,
      [
        alert.id ?? crypto.randomUUID(),
        alert.siteId,
        alert.customerId,
        alert.dedupeKey,
        alert.code,
        alert.serial,
        alert.openedAt,
        alert.status,
        alert.detectedAt,
        alert.notifiedAt,
        null,
      ],
    );
    return alertRow(result.rows[0]);
  }

  async unnotifiedAlerts(customerId: string): Promise<AlertRecord[]> {
    const result = await this.db.query(
      "select * from alerts where customer_id = $1 and notified_at is null and status = 'OPEN'",
      [customerId],
    );
    return result.rows.map(alertRow);
  }

  async markAlertNotified(id: string, notifiedAt: string): Promise<void> {
    await this.db.query("update alerts set notified_at = $2 where id = $1", [id, notifiedAt]);
  }

  async addLlmUsage(input: { customerId: string; model: string; inputTokens: number; outputTokens: number; at: string }): Promise<void> {
    await this.db.query(
      `insert into llm_usage (id, customer_id, at, model, input_tokens, output_tokens)
       values ($1,$2,$3,$4,$5,$6)`,
      [crypto.randomUUID(), input.customerId, input.at, input.model, input.inputTokens, input.outputTokens],
    );
  }

  async llmTokensSince(customerId: string, sinceIso: string): Promise<number> {
    const result = await this.db.query(
      `select coalesce(sum(input_tokens + output_tokens), 0)::int as tokens
         from llm_usage where customer_id = $1 and at >= $2`,
      [customerId, sinceIso],
    );
    return Number(result.rows[0]?.tokens ?? 0);
  }

  async createPayment(input: Omit<PaymentRecord, "id" | "createdAt" | "status" | "currency"> & { id?: string }): Promise<PaymentRecord> {
    const id = input.id ?? crypto.randomUUID();
    const result = await this.db.query(
      `insert into payments (id, customer_id, amount_agorot, currency, status, provider, description)
       values ($1,$2,$3,'ILS','stub_pending',$4,$5) returning *`,
      [id, input.customerId, input.amountAgorot, input.provider, input.description],
    );
    return payment(result.rows[0]);
  }

  async getPayment(id: string): Promise<PaymentRecord | null> {
    const result = await this.db.query("select * from payments where id = $1", [id]);
    return result.rows[0] ? payment(result.rows[0]) : null;
  }

  async adminRows(): Promise<AdminCustomerRow[]> {
    const customers = await this.listCustomers();
    const month = new Date();
    month.setUTCDate(1);
    month.setUTCHours(0, 0, 0, 0);
    const since = month.toISOString();
    const rows: AdminCustomerRow[] = [];
    for (const person of customers) {
      const sites = await this.listSites(person.id);
      const last = await this.db.query(
        "select max(pulled_at) as pulled_at from raw_pulls where customer_id = $1",
        [person.id],
      );
      rows.push({
        customer: person,
        sites,
        lastPullAt: last.rows[0]?.pulled_at ? new Date(String(last.rows[0].pulled_at)).toISOString() : null,
        creditsThisMonth: await this.creditsSince(person.id, since),
        llmTokensThisMonth: await this.llmTokensSince(person.id, since),
        failedRefreshes: await Promise.all(sites.map((item) => this.tokenMeta(item.id))),
      });
    }
    return rows;
  }
}

function summary(row: Record<string, unknown>): SummaryRecord {
  return {
    id: String(row.id),
    customerId: String(row.customer_id),
    localDate: String(row.local_date).slice(0, 10),
    payload: row.payload as DailySummary,
    text: String(row.text),
    createdAt: new Date(String(row.created_at)).toISOString(),
    sentAt: row.sent_at ? new Date(String(row.sent_at)).toISOString() : null,
  };
}

function alertRow(row: Record<string, unknown>): AlertRecord {
  return {
    id: String(row.id),
    siteId: String(row.site_id),
    customerId: String(row.customer_id),
    dedupeKey: String(row.dedupe_key),
    code: String(row.code),
    serial: row.serial == null ? null : String(row.serial),
    openedAt: new Date(String(row.opened_at)).toISOString(),
    status: String(row.status),
    detectedAt: new Date(String(row.detected_at)).toISOString(),
    notifiedAt: row.notified_at ? new Date(String(row.notified_at)).toISOString() : null,
  };
}

function payment(row: Record<string, unknown>): PaymentRecord {
  return {
    id: String(row.id),
    customerId: String(row.customer_id),
    amountAgorot: Number(row.amount_agorot),
    currency: "ILS",
    status: "stub_pending",
    provider: String(row.provider),
    description: String(row.description),
    createdAt: new Date(String(row.created_at)).toISOString(),
  };
}
