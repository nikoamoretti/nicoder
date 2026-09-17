import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  timeout: 30_000,
  use: {
    baseURL: process.env.TASKBOARD_URL || "http://127.0.0.1:4173",
    headless: true,
  },
});
