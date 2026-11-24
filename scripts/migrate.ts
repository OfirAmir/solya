import { readFileSync } from "node:fs";
import path from "node:path";
import { Pool } from "pg";

const databaseUrl = process.env.DATABASE_URL;
if (!databaseUrl) {
  console.error("DATABASE_URL is required.");
  process.exit(1);
}
const local = /localhost|127\.0\.0\.1/.test(databaseUrl);
const pool = new Pool({
  connectionString: databaseUrl,
  ssl: local ? undefined : { rejectUnauthorized: false },
});
const sql = readFileSync(path.join(process.cwd(), "db", "schema.sql"), "utf8");
await pool.query(sql);
await pool.end();
console.log("schema applied");
