# pung-scrim-bot

A small Python starter project for building and running Python programs.

## Run & Operate

- `python main.py` — run the Python starter program
- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- Python 3.12
- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` — Python entry point
- `requirements.txt` — Python dependencies
- `README.md` — quick-start instructions

## Architecture decisions

- Keep the Discord bot's existing SQLite storage; do not replace it with the unrelated API server's database.
- A scrim belongs to one Discord server and uses its own public and staff channels. Never resolve a missing context to an arbitrary scrim.
- This Replit workspace is the beta bot environment. Never connect it to, auto-deploy it to, or upload from it to the Bot-hosting.net instance running the alpha bot unless the user explicitly authorizes that specific release.

## Product

_Describe the high-level user-facing capabilities of this app once they exist._

## User preferences

- All bot-authored text visible to managers must be in English, including the public board, private interactions, errors, and public command replies. Keep manager-facing slot rows concise: do not append “Awaiting manager”. Staff-only messages and internal logs may remain French.
- New staff panels should follow the visual style and navigation conventions of `!setup`, while remaining separate commands when requested.

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
