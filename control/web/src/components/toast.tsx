import {createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode} from "react";
import {createPortal} from "react-dom";

type ToastKind = "success" | "error" | "info";
type ToastItem = {id: number; kind: ToastKind; message: string};
type Toasts = {success(message: string): void; error(message: string): void; info(message: string): void};

const LIFETIME_MS: Record<ToastKind, number> = {success: 5000, info: 5000, error: 10000};
const IGNORE: Toasts = {success() {}, error() {}, info() {}};
const ToastContext = createContext<Toasts>(IGNORE);

/** Outcome notices for actions: success, error (with the request id in the message) and info such as "queued". */
export function useToast(): Toasts {
  return useContext(ToastContext);
}

function Toast({item, onDismiss}: {item: ToastItem; onDismiss(id: number): void}) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const timer = setTimeout(() => onDismiss(item.id), LIFETIME_MS[item.kind]);
    return () => clearTimeout(timer);
  }, [item, onDismiss, paused]);
  return <li className={`toast toast-${item.kind}`} data-kind={item.kind}
    onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}
    onFocus={() => setPaused(true)} onBlur={() => setPaused(false)}>
    <span className="toast-kind">{item.kind === "success" ? "Done" : item.kind === "error" ? "Failed" : "Note"}</span>
    <span className="toast-message">{item.message}</span>
    <button type="button" className="toast-dismiss" onClick={() => onDismiss(item.id)} aria-label="Dismiss notification">×</button>
  </li>;
}

export function ToastProvider({children}: {children: ReactNode}) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(0);
  const dismiss = useCallback((id: number) => setItems(current => current.filter(item => item.id !== id)), []);
  const push = useCallback((kind: ToastKind, message: string) => {
    setItems(current => [...current.slice(-4), {id: next.current++, kind, message}]);
  }, []);
  const toasts = useMemo<Toasts>(() => ({
    success: message => push("success", message),
    error: message => push("error", message),
    info: message => push("info", message),
  }), [push]);
  return <ToastContext.Provider value={toasts}>
    {children}
    {createPortal(<ol className="toast-region" role="region" aria-label="Notifications" aria-live="polite" aria-relevant="additions">{items.map(item => <Toast key={item.id} item={item} onDismiss={dismiss}/>)}</ol>, document.body)}
  </ToastContext.Provider>;
}
