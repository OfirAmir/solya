import { NextResponse } from "next/server";
import { loadConfig } from "@/lib/config";
import { handleCustomerMessage } from "@/lib/messaging/guard";
import { createTelegramAdapter } from "@/lib/messaging/telegram";
import { getStore } from "@/lib/store";

export async function POST(request: Request) {
  const config = loadConfig();
  if (!config.telegram.botToken) {
    return NextResponse.json({ error: "telegram is not configured" }, { status: 503 });
  }
  if (config.telegram.webhookSecret) {
    const secret = request.headers.get("x-telegram-bot-api-secret-token");
    if (secret !== config.telegram.webhookSecret) {
      return NextResponse.json({ error: "unauthorized" }, { status: 401 });
    }
  }
  const raw: unknown = await request.json().catch(() => null);
  const adapter = createTelegramAdapter(config.telegram.botToken);
  const message = adapter.receive(raw);
  if (!message) return NextResponse.json({ ok: true, ignored: true });
  const result = await handleCustomerMessage(message, getStore(), config);
  if (message.chatId) {
    for (const reply of result.replies) {
      await adapter.sendText(message.chatId, reply);
    }
  }
  return NextResponse.json({ ok: true, actions: result.actions });
}
