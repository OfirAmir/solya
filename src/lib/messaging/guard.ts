import type { AppConfig } from "../config";
import { WELCOME_MESSAGES } from "../rules/hebrew";
import type { Store } from "../store/types";
import type { InboundMessage } from "./adapter";
import { answerFromStore, refusalText } from "./qa";
import { answerWithOpenRouter } from "../llm/openrouter";

export type HandleResult = {
  replies: string[];
  /** Customer text never becomes an account action. This stays empty. */
  actions: [];
};

const START = /^\/start(?:@\w+)?(?:\s+([A-Za-z0-9_-]+))?$/;

/**
 * Inbound chat is data. Payments, cancellation, token changes, deletion,
 * and service calls are not reachable from this function.
 */
export async function handleCustomerMessage(
  message: InboundMessage,
  store: Store,
  config: AppConfig,
  now = new Date(),
): Promise<HandleResult> {
  const start = message.text.trim().match(START);
  if (start?.[1]) {
    const customer = await store.getCustomerByLinkToken(start[1]);
    if (!customer) return { replies: ["הקישור לא מזוהה. צריך להתחיל מההרשמה באתר."], actions: [] };
    await store.setTelegramChat(customer.id, message.chatId);
    return { replies: [...WELCOME_MESSAGES], actions: [] };
  }

  const customer = await customerForChat(store, message.chatId);
  if (!customer) {
    return { replies: ["עוד אין כאן מערכת מחוברת. הקישור מהאתר מחבר את החשבון."], actions: [] };
  }

  if (asksForAccountAction(message.text)) {
    const reply = config.solMayOpenServiceCalls
      ? "אני לא פותחת קריאת שירות, ביטול או החזר מתוך הודעה. פעולה בחשבון דורשת אישור מחוץ לשיחה."
      : "אני לא מבצעת פעולות בחשבון מהשיחה. אני ממליצה, והפעולה עצמה נשארת אצלך.";
    return { replies: [reply], actions: [] };
  }

  const summary = await store.latestSummary(customer.id);
  const pulls = await store.listPulls((await store.listSites(customer.id))[0]?.id ?? "");
  const lastPullAt = pulls.map((pull) => pull.pulledAt).sort().at(-1) ?? null;
  const stored = answerFromStore(message.text, summary, lastPullAt);
  if (stored.handled) return { replies: [stored.text], actions: [] };

  if (!config.llm.enabled) return { replies: [refusalText()], actions: [] };

  const since = monthStart(now);
  const used = await store.llmTokensSince(customer.id, since);
  const llm = await answerWithOpenRouter({
    config,
    question: message.text,
    usedTokens: used,
    context: summary
      ? {
          localDate: summary.localDate,
          totalProductionWh: summary.payload.totalProductionWh,
          totalIncomeAgorot: summary.payload.totalIncomeAgorot,
          overallRay: summary.payload.overallRay,
          sites: summary.payload.sites.map((site) => ({
            name: site.cardName,
            productionWh: site.productionWh,
            incomeAgorot: site.incomeAgorot,
            ray: site.ray,
            communicationLost: site.communicationLost,
          })),
        }
      : null,
  });
  if (llm.inputTokens + llm.outputTokens > 0) {
    await store.addLlmUsage({
      customerId: customer.id,
      model: config.llm.model || "unconfigured",
      inputTokens: llm.inputTokens,
      outputTokens: llm.outputTokens,
      at: now.toISOString(),
    });
  }
  return { replies: [sanitizeModelReply(llm.text)], actions: [] };
}

function asksForAccountAction(text: string): boolean {
  return /בטל|ביטול|החזר|refund|cancel|מחק|delete|פתח(י)? קריאה|קריאת שירות|service call|תשלום|charge|חיוב|ignore previous|system:/i.test(
    text,
  );
}

function sanitizeModelReply(text: string): string {
  if (/ביטלתי|פתחתי קריאה|זיכיתי|החזרתי/.test(text)) {
    return "אני לא מבצעת פעולות בחשבון מהשיחה. אני יכולה לענות על נתוני הייצור השמורים.";
  }
  return text;
}

async function customerForChat(store: Store, chatId: string) {
  const customers = await store.listCustomers();
  return customers.find((customer) => customer.telegramChatId === chatId) ?? null;
}

function monthStart(now: Date): string {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
}
