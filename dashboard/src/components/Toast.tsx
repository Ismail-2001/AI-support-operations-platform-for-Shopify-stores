import { createContext, useContext, useState, useCallback, type ReactNode } from "react";
import { X, CheckCircle, AlertCircle, Info } from "lucide-react";

type ToastType = "success" | "error" | "info";

interface Toast {
  id: string;
  type: ToastType;
  message: string;
}

interface ToastContextValue {
  toast: (type: ToastType, message: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}

const ICONS: Record<ToastType, typeof CheckCircle> = {
  success: CheckCircle,
  error: AlertCircle,
  info: Info,
};

const STYLES: Record<ToastType, string> = {
  success: "bg-teal-100 text-teal-700 border-teal-200 dark:bg-teal/20 dark:text-teal dark:border-teal/30",
  error: "bg-rose-100 text-rose-700 border-rose-200 dark:bg-rose/20 dark:text-rose dark:border-rose/30",
  info: "bg-ink-900/[0.05] text-ink-700 border-line dark:bg-white/5 dark:text-ink-dark-700 dark:border-line-dark",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const toast = useCallback((type: ToastType, message: string) => {
    const id = crypto.randomUUID();
    setToasts((prev) => [...prev, { id, type, message }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
        {toasts.map((t) => {
          const Icon = ICONS[t.type];
          return (
            <div
              key={t.id}
              className={`flex items-start gap-3 px-4 py-3 rounded-xl2 border shadow-panel animate-in slide-in-from-right-5 ${STYLES[t.type]}`}
            >
              <Icon className="w-4 h-4 mt-0.5 shrink-0" />
              <p className="text-sm flex-1">{t.message}</p>
              <button onClick={() => dismiss(t.id)} className="text-current opacity-50 hover:opacity-100">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
