import { decodeEncryptionKey } from "../crypto";
import { loadConfig } from "../config";
import { MemoryStore } from "./memory";
import { getPool, PostgresStore } from "./postgres";
import type { Store } from "./types";

const globalStore = globalThis as unknown as { __solyaStore?: MemoryStore };

export function getStore(): Store {
  const config = loadConfig();
  if (!config.tokenEncryptionKey) {
    throw new Error("TOKEN_ENCRYPTION_KEY is required. Generate one with: openssl rand -base64 32");
  }
  const key = decodeEncryptionKey(config.tokenEncryptionKey);
  if (config.databaseUrl) {
    return new PostgresStore(getPool(config.databaseUrl), key);
  }
  if (!globalStore.__solyaStore) globalStore.__solyaStore = new MemoryStore(key);
  return globalStore.__solyaStore;
}

export function resetMemoryStore(): void {
  globalStore.__solyaStore = undefined;
}
