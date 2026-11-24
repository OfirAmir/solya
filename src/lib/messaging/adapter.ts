export type InboundMessage = {
  channel: "telegram" | "whatsapp";
  chatId: string;
  text: string;
  receivedAt: string;
};

export interface MessagingAdapter {
  readonly channel: "telegram" | "whatsapp";
  sendText(chatId: string, text: string): Promise<void>;
  sendCard(chatId: string, png: Uint8Array, caption?: string): Promise<void>;
  receive(raw: unknown): InboundMessage | null;
}

/** Phase 2 fills this in. MVP 0 does not send WhatsApp or pick Twilio. */
export function createWhatsAppAdapter(): MessagingAdapter {
  const pending = async () => {
    throw new Error("WhatsApp is not enabled in MVP 0. The adapter is reserved for a later provider.");
  };
  return {
    channel: "whatsapp",
    sendText: pending,
    sendCard: pending,
    receive(): InboundMessage | null {
      return null;
    },
  };
}
