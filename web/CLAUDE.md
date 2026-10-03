# web/

`web/` is the frontend (SPA). It uses React, HeroUI v3 (on Tailwind CSS v4) and Vite+ (`vp`).
It calls the application server (`api/`) with Connect.

## Commands

```bash
(cd web && pnpm install)
(cd web && pnpm exec vp check)  # format, lint and type check
(cd web && pnpm build)
(cd web && pnpm dev)            # Start nanashi-api on 127.0.0.1:8080 first
(cd web && pnpm generate)       # Make api/gen/ and web/src/gen/ from proto/
```

## Rules

- Use pnpm. Do not use npm. With npm, the install of `vite-plus` fails.
- Use the `vp` commands (`vp dev`, `vp build`, `vp check`). Import from `vite-plus`, not from `vite`.
- Use the HeroUI components before you write a new component. HeroUI v3 does not need a provider.
- Do not edit `src/gen/`. `pnpm generate` makes it.
- In development, the Vite dev server sends `/nanashi.v1.*` to `nanashi-api`. Thus, the API does not need CORS.
