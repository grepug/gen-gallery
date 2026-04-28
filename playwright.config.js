const path = require("path");
const { defineConfig } = require("@playwright/test");

const repoRoot = __dirname;
const venvPython = path.join(
  process.env.HOME || "",
  ".imagegen-server",
  "venv",
  "bin",
  "python",
);
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:8010";

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  fullyParallel: false,
  reporter: "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: `${venvPython} -m imagegen_server`,
        cwd: repoRoot,
        env: {
          ...process.env,
          PYTHONPATH: path.join(repoRoot, "src"),
          IMAGEGEN_SERVER_HOME: path.join(repoRoot, ".playwright-runtime"),
          IMAGE_API_KEYS_JSON: '[{"name":"key-a","api_key":"sk-test"}]',
          OPENAI_BASE_URL: "https://api.example.com/v1",
          APP_HOST: "127.0.0.1",
          APP_PORT: "8010",
        },
        url: baseURL,
        reuseExistingServer: false,
        timeout: 30_000,
      },
});
