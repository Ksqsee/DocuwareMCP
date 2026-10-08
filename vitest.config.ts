import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: { bindings: { DW_URL: "https://dw.test", PUBLIC_URL: "https://docuware-mcp.test" } },
    }),
  ],
  // The first test in a file also pays for starting workerd.
  test: { testTimeout: 20_000 },
});
