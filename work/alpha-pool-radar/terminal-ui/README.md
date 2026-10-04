# Dashboard

React operator UI for Volume Bot. Talks to the API at `http://localhost:8787` (configurable via Vite env).

## Development

From repo root:

```bash
npm run stack          # API + dashboard together
# or
npm run api            # terminal 1
npm run dashboard      # terminal 2
```

Open http://localhost:5173

## Build

```bash
npm run dashboard:build
```

Output: `dashboard/dist/` — serve statically behind the API or any static host in production.

## Stack

- React 19 + TypeScript
- Vite 8
- TanStack Query (polling, cache)
- Tailwind CSS 4
- Recharts (session charts)
- Radix UI (dialogs)

## Tabs

| Tab | Purpose |
|-----|---------|
| Overview | Balances, pool info, efficiency, start/stop |
| Config | Active session parameters |
| Sessions | Historical runs and charts |
| Wallets | Cycle wallet files and balances |
| Output | Live bot log tail |

## API proxy

Vite dev server proxies `/api` to port 8787. See `dashboard/vite.config.ts`.

## Documentation

Full project docs: [../README.md](../README.md) and [../docs/](../docs/).
