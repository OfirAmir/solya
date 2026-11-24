import type { InboundMessage, MessagingAdapter } from "./adapter";

type FetchLike = typeof fetch;

export function createTelegramAdapter(botToken: string, fetchImpl: FetchLike = fetch): MessagingAdapter {
  const base = `https://api.telegram.org/bot${botToken}`;
  return {
    channel: "telegram",
    async sendText(chatId, text) {
      const response = await fetchImpl(`${base}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text }),
      });
      if (!response.ok) throw new Error(`Telegram sendMessage failed with status ${response.status}.`);
    },
    async sendCard(chatId, png, caption) {
      const form = new FormData();
      form.set("chat_id", chatId);
      if (caption) form.set("caption", caption);
      form.set("photo", new File([Buffer.from(png)], "solya.png", { type: "image/png" }));
      const response = await fetchImpl(`${base}/sendPhoto`, { method: "POST", body: form });
      if (!response.ok) throw new Error(`Telegram sendPhoto failed with status ${response.status}.`);
    },
    receive(raw) {
      return parseTelegramUpdate(raw);
    },
  };
}

export function parseTelegramUpdate(raw: unknown): InboundMessage | null {
  if (!raw || typeof raw !== "object") return null;
  const message = (raw as { message?: unknown }).message;
  if (!message || typeof message !== "object") return null;
  const text = (message as { text?: unknown }).text;
  const chat = (message as { chat?: { id?: unknown } }).chat;
  const date = (message as { date?: unknown }).date;
  if (typeof text !== "string" || !text.trim() || chat?.id == null) return null;
  const seconds = typeof date === "number" ? date : Date.now() / 1000;
  return {
    channel: "telegram",
    chatId: String(chat.id),
    text,
    receivedAt: new Date(seconds * 1000).toISOString(),
  };
}

export function telegramDeepLink(botUsername: string, linkToken: string): string | null {
  if (!botUsername) return null;
  return `https://t.me/${botUsername}?start=${encodeURIComponent(linkToken)}`;
}
