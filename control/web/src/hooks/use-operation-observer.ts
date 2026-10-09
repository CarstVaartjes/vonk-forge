import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/errors";

/**
 * The one way the UI watches remote work.
 *
 * Three things are kept apart on purpose:
 *  - the remote state (the operation's own succeeded / failed / cancelled),
 *  - "still running, we have watched a long time" (`background`), and
 *  - "the Controller cannot be reached right now" (`connection`), which keeps
 *    the last known value and its age instead of failing the operation.
 * A local failure or deadline never turns into a remote failure.
 */
export const OBSERVE_INTERVAL_MS = 1_000;
export const OBSERVE_MAX_INTERVAL_MS = 15_000;
export const OBSERVE_DEADLINE_MS = 180_000;

export type ObserverConnection = "live" | "reconnecting";

/** Exponential backoff after consecutive failures, bounded by `max`. */
export function backoffDelay(
  failures: number,
  base = OBSERVE_INTERVAL_MS,
  max = OBSERVE_MAX_INTERVAL_MS,
): number {
  return failures <= 0 ? base : Math.min(base * 2 ** Math.min(failures, 16), max);
}

type Options<T> = {
  isTerminal(value: T): boolean;
  /** Runs once when the remote state is terminal: invalidate and refetch dependent data here. */
  onTerminal?(value: T): void;
  intervalMs?: number;
  deadlineMs?: number;
};

export function useOperationObserver<T>({
  isTerminal,
  onTerminal,
  intervalMs = OBSERVE_INTERVAL_MS,
  deadlineMs = OBSERVE_DEADLINE_MS,
}: Options<T>) {
  const [value, setValue] = useState<T>();
  const [connection, setConnection] = useState<ObserverConnection>("live");
  const [lastSuccessAt, setLastSuccessAt] = useState<number>();
  const [background, setBackground] = useState(false);
  const [fatal, setFatal] = useState("");
  const [run, setRun] = useState<{ id: number; fetch(signal: AbortSignal): Promise<T> }>();
  const runId = useRef(0);
  const lastFetch = useRef<((signal: AbortSignal) => Promise<T>) | undefined>(undefined);
  const latest = useRef({ isTerminal, onTerminal });
  latest.current = { isTerminal, onTerminal };

  /** Begin observing from `initial`; `fetch` reads the operation's current state. */
  const start = useCallback((initial: T, fetch: (signal: AbortSignal) => Promise<T>) => {
    lastFetch.current = fetch;
    setValue(initial);
    setConnection("live");
    setLastSuccessAt(Date.now());
    setBackground(false);
    setFatal("");
    if (latest.current.isTerminal(initial)) {
      setRun(undefined);
      latest.current.onTerminal?.(initial);
      return;
    }
    runId.current += 1;
    setRun({ id: runId.current, fetch });
  }, []);
  const reset = useCallback(() => {
    lastFetch.current = undefined;
    setRun(undefined);
    setValue(undefined);
    setConnection("live");
    setLastSuccessAt(undefined);
    setBackground(false);
    setFatal("");
  }, []);
  const resume = useCallback(() => {
    if (!lastFetch.current) return;
    runId.current += 1;
    setBackground(false);
    setFatal("");
    setRun({ id: runId.current, fetch: lastFetch.current });
  }, []);

  useEffect(() => {
    if (!run) return;
    const controller = new AbortController();
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = (delay: number) => {
      timer = setTimeout(() => {
        timer = undefined;
        void tick();
      }, delay);
    };
    async function tick(): Promise<void> {
      try {
        const next = await run!.fetch(controller.signal);
        if (controller.signal.aborted) return;
        failures = 0;
        setConnection("live");
        setLastSuccessAt(Date.now());
        setValue(next);
        if (latest.current.isTerminal(next)) {
          setRun(undefined);
          latest.current.onTerminal?.(next);
          return;
        }
        schedule(intervalMs);
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          setFatal(error.message);
          setRun(undefined);
          return;
        }
        failures += 1;
        setConnection("reconnecting");
        schedule(backoffDelay(failures, intervalMs));
      }
    }
    const deadline = setTimeout(() => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
      setBackground(true);
      setRun(undefined);
    }, deadlineMs);
    // Connectivity returning is the cue to look again now, not at the next backoff step.
    const resume = () => {
      if (timer === undefined) return;
      clearTimeout(timer);
      timer = undefined;
      void tick();
    };
    addEventListener("online", resume);
    schedule(intervalMs);
    return () => {
      clearTimeout(deadline);
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
      removeEventListener("online", resume);
    };
  }, [run, intervalMs, deadlineMs]);

  return {
    value,
    connection,
    lastSuccessAt,
    background,
    fatal,
    observing: run !== undefined,
    start,
    reset,
    resume,
  };
}

export function ageLabel(lastSuccessAt: number | undefined, now: number): string {
  if (lastSuccessAt === undefined) return "never";
  const seconds = Math.max(0, Math.round((now - lastSuccessAt) / 1000));
  return seconds < 120 ? `${seconds} s ago` : `${Math.round(seconds / 60)} min ago`;
}
