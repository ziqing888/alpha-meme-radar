import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import reportFixture from "../tests/fixtures/monitor-v3-report.json";
import App from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("live monitor workspace status", () => {
  it("shows the live monitor timestamp instead of the stale full-report timestamp", async () => {
    const monitor = structuredClone(reportFixture);
    monitor.observed_at = "2026-09-14T11:55:12+08:00";
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const payload = url.includes("monitor-v3.json")
        ? { monitor_v3: monitor, monitor_baselines: {}, monitor_intelligence_rows: [] }
        : {
            meta: { report_generated_at: "2026-09-12T03:53:55+08:00" },
            alpha_rows: [],
            meme_rows: [],
            dealer_rows: [],
            recommendation_rows: [],
          };
      return {
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "MEME 监控" }));

    expect(await screen.findByText(/更新于 2026-09-14T11:55:12\+08:00/)).toBeVisible();
    expect(screen.queryByText(/更新于 2026-09-12T03:53:55\+08:00/)).not.toBeInTheDocument();
  });
});
