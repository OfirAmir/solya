import type { TokenSet } from "../solaredge/types";

export type SavedToken = {
  accessToken: string;
  refreshToken: string;
  accessExpiresAt: string | null;
};

export type TokenStore = {
  read(siteId: string): Promise<SavedToken | null>;
  /** Persist the rotated pair before any later data call. */
  replace(siteId: string, token: SavedToken): Promise<void>;
  /** Records the failure and leaves the previous pair in place. */
  recordFailure(siteId: string, error: string): Promise<void>;
};

export type RefreshResult =
  | { ok: true; accessToken: string }
  | { ok: false; error: string };

/**
 * SolarEdge rotates the refresh token on every refresh and invalidates the old one.
 * The new pair is written before it is used. A failed refresh does not clear it.
 */
export async function refreshAndPersist(
  siteId: string,
  store: TokenStore,
  refresh: (refreshToken: string) => Promise<TokenSet>,
  now = new Date(),
): Promise<RefreshResult> {
  const current = await store.read(siteId);
  if (!current) return { ok: false, error: "no token stored" };
  let next: TokenSet;
  try {
    next = await refresh(current.refreshToken);
  } catch (error) {
    const message = error instanceof Error ? error.message : "refresh failed";
    await store.recordFailure(siteId, message);
    return { ok: false, error: message };
  }
  const saved: SavedToken = {
    accessToken: next.accessToken,
    refreshToken: next.refreshToken,
    accessExpiresAt: new Date(now.getTime() + next.expiresIn * 1000).toISOString(),
  };
  await store.replace(siteId, saved);
  return { ok: true, accessToken: saved.accessToken };
}
