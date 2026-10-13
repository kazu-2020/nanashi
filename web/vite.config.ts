import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, lazyPlugins } from "vite-plus";

export default defineConfig({
  fmt: { ignorePatterns: ["src/gen/**"] },
  lint: {
    ignorePatterns: ["src/gen/**"],
    plugins: ["react", "typescript", "oxc"],
    rules: {
      "react/rules-of-hooks": "error",
      "react/only-export-components": ["warn", { allowConstantExport: true }],
      "vite-plus/prefer-vite-plus-imports": "error",
    },
    options: { typeAware: true, typeCheck: true },
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
  },
  plugins: lazyPlugins(() => [react(), tailwindcss()]),
  // The dev server sends the Connect requests to nanashi-api (api/).
  server: { proxy: { "/nanashi.v1.": "http://127.0.0.1:8080" } },
});
