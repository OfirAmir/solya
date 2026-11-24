import { formatIlsFromAgorot, formatKwhFromWh } from "../format";
import type { SummaryRecord } from "../store/types";

export type QaAnswer = {
  text: string;
  handled: boolean;
};

/**
 * Answers a small set of questions from the stored summary only.
 * Anything else stays unhandled so the optional LLM can try, or we refuse.
 */
export function answerFromStore(question: string, summary: SummaryRecord | null, lastPullAt: string | null): QaAnswer {
  const text = question.trim();
  if (!summary) {
    return { handled: true, text: "המידע עדיין לא הגיע אלי, אני על זה." };
  }
  const payload = summary.payload;
  if (/התרא/.test(text)) {
    if (payload.openAlertCount === 0) return { handled: true, text: "אין התראות פעילות." };
    return { handled: true, text: `יש ${payload.openAlertCount} התראות פתוחות בנתונים השמורים.` };
  }
  if (/משיכ|עודכ|נתונים/.test(text) && /מתי|אחרון/.test(text)) {
    return {
      handled: true,
      text: lastPullAt ? `המשיכה האחרונה נשמרה ב-${lastPullAt}.` : "המידע עדיין לא הגיע אלי, אני על זה.",
    };
  }
  if (/צריכה עצמית|עצמית/.test(text)) {
    const lines = payload.sites.map((site) => {
      if (site.productionWh == null || site.exportWh == null) {
        return `${site.cardName}: אין קריאת יצוא שמורה, אז אין חישוב.`;
      }
      const self = site.productionWh - site.exportWh;
      return `${site.cardName}: ${formatKwhFromWh(self, 2)} קוט״ש, חישוב של ייצור פחות יצוא ולא מונה נפרד.`;
    });
    return { handled: true, text: lines.join("\n") };
  }
  if (/הכנס|שקל|₪/.test(text)) {
    if (payload.totalIncomeAgorot == null) return { handled: true, text: "המידע עדיין לא הגיע אלי, אני על זה." };
    return { handled: true, text: `ההכנסה השמורה היא ${formatIlsFromAgorot(payload.totalIncomeAgorot)} ₪.` };
  }
  if (/RAY|ריי|ציון/i.test(text)) {
    if (payload.overallRay == null) return { handled: true, text: "המידע עדיין לא הגיע אלי, אני על זה." };
    return { handled: true, text: `הציון RAY FACTOR השמור עומד על ${payload.overallRay}.` };
  }
  if (/ייצור|יוצר|קוט/.test(text)) {
    if (payload.totalProductionWh == null) return { handled: true, text: "המידע עדיין לא הגיע אלי, אני על זה." };
    return { handled: true, text: `הייצור השמור הוא ${formatKwhFromWh(payload.totalProductionWh, 1)} קוט״ש.` };
  }
  return { handled: false, text: "" };
}

export function refusalText(): string {
  return "אני עונה רק מתוך הנתונים השמורים. את השאלה הזו אני עדיין לא יודעת לחשב מהם.";
}
