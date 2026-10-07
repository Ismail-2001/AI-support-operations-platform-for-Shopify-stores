import { useCallback, useEffect, useState } from "react";
import type { Connection } from "./api";

const STORAGE_KEY = "cs-agent-console-connection";

/**
 * Parse a stored connection defensively. A corrupt or legacy value (hand-edited
 * JSON, a truncated write, an old shape) previously threw inside the useState
 * initializer and white-screened the whole dashboard. Anything unusable is
 * removed so the Connect screen shows instead of an error overlay.
 */
export function parseConnection(raw: string | null): Connection | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      parsed !== null &&
      typeof parsed === "object" &&
      typeof (parsed as Connection).baseUrl === "string" &&
      typeof (parsed as Connection).apiKey === "string" &&
      (parsed as Connection).baseUrl.length > 0
    ) {
      return parsed as Connection;
    }
  } catch {
    /* fall through — reset below */
  }
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
  return null;
}

function readStoredConnection(): Connection | null {
  try {
    return parseConnection(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

export function useConnection() {
  const [connection, setConnectionState] = useState<Connection | null>(readStoredConnection);

  const setConnection = useCallback((conn: Connection | null) => {
    setConnectionState(conn);
    try {
      if (conn) localStorage.setItem(STORAGE_KEY, JSON.stringify(conn));
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* storage unavailable (private mode) — still works for this session */
    }
  }, []);

  useEffect(() => {
    // keep other tabs in sync if the key changes
    const listener = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) {
        setConnectionState(parseConnection(e.newValue));
      }
    };
    window.addEventListener("storage", listener);
    return () => window.removeEventListener("storage", listener);
  }, []);

  return { connection, setConnection };
}
