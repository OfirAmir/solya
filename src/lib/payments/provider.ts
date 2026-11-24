import type { Store } from "../store/types";

export type Checkout = {
  id: string;
  url: string;
  status: "stub_pending";
  provider: string;
};

export interface PaymentProvider {
  readonly id: string;
  createCheckout(input: {
    customerId: string;
    amountAgorot: number;
    description: string;
  }): Promise<Checkout>;
}

/**
 * No payment provider was chosen (handover 5.5). The stub records an intent
 * and never captures money, marks a subscription active, or issues a refund.
 */
export function createPaymentProvider(providerId: string, store: Store, appUrl: string): PaymentProvider {
  if (providerId !== "stub") {
    throw new Error(
      `Payment provider "${providerId}" is not implemented. SOLYA_PAYMENT_PROVIDER stays "stub" until one is chosen.`,
    );
  }
  return {
    id: "stub",
    async createCheckout(input) {
      const row = await store.createPayment({
        customerId: input.customerId,
        amountAgorot: input.amountAgorot,
        provider: "stub",
        description: input.description,
      });
      await store.setSubscription(input.customerId, "pending_payment");
      const root = appUrl || "";
      return {
        id: row.id,
        url: `${root}/pay/${row.id}`,
        status: "stub_pending",
        provider: "stub",
      };
    },
  };
}
