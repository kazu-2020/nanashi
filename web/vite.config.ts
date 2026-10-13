import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import { defineConfig, lazyPlugins } from "vite-plus";

export default defineConfig({
  fmt: { ignorePatterns: ["src/gen/**", "src/routeTree.gen.ts"] },
  lint: {
    ignorePatterns: ["src/gen/**", "src/routeTree.gen.ts"],
    plugins: ["react", "typescript", "oxc"],
    rules: {
      "react/rules-of-hooks": "error",
      "react/only-export-components": ["warn", { allowConstantExport: true }],
      "vite-plus/prefer-vite-plus-imports": "error",
    },
    options: { typeAware: true, typeCheck: true },
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
  },
  // The router plugin makes src/routeTree.gen.ts from src/routes/. It must come before react().
  plugins: lazyPlugins(() => [
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    react(),
    tailwindcss(),
  ]),
  // The dev server sends the Connect requests to nanashi-api (api/).
  server: { proxy: { "/nanashi.v1.": "http://127.0.0.1:8080" } },
});
