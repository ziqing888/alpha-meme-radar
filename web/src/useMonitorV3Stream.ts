import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { RadarReport } from "./monitorStreamTypes";

function streamUrlFor(reportUrl: string): string {
  if (typeof window === "undefined") return "/api/monitor-stream";
  const local = window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost";
  if (local && (reportUrl === "auto" || reportUrl.includes("127.0.0.1") || reportUrl.includes("localhost"))) {
    // local page still prefers cloud stream only when hosted remotely; keep cloud path for pages.dev
  }
  return "/api/monitor-stream";
}

export function useMonitorV3Stream(reportUrl: string, queryKey: readonly unknown[]) {
  const queryClient = useQueryClient();
  const [live, setLive] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof EventSource === "undefined") return;
    // Only use SSE on non-local hosts (Cloudflare Pages). Local :5173 keeps direct local fetch.
    const host = window.location.hostname;
    const isLocal = host === "127.0.0.1" || host === "localhost";
    if (isLocal) {
      setLive(false);
      return;
    }

    let cancelled = false;
    const connect = () => {
      if (cancelled) return;
      const es = new EventSource(streamUrlFor(reportUrl));
      esRef.current = es;
      es.onopen = () => {
        if (!cancelled) setLive(true);
      };
      es.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as RadarReport;
          if (!payload || typeof payload !== "object") return;
          queryClient.setQueryData(queryKey, payload);
        } catch {
          // ignore malformed chunks
        }
      };
      es.onerror = () => {
        setLive(false);
        es.close();
        esRef.current = null;
      };
    };

    connect();
    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
      setLive(false);
    };
  }, [queryClient, reportUrl]); // queryKey identity stabilized by caller useMemo

  return live;
}
