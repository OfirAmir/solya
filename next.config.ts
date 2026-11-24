import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  serverExternalPackages: ["@napi-rs/canvas", "pg"],
  outputFileTracingIncludes: {
    "/api/cron/daily": ["./assets/fonts/**/*"],
    "/api/cron/early": ["./assets/fonts/**/*"],
    "/api/telegram/webhook": ["./assets/fonts/**/*"],
  },
};

export default nextConfig;
