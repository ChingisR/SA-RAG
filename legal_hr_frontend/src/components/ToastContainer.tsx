import { useState, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { CheckCircle, AlertCircle, Clock, X } from 'lucide-react';
import { cn } from '../lib/cn';

export interface Toast {
  id: string;
  message: string;
  type: 'success' | 'error' | 'info';
}

// ── Hook ─────────────────────────────────────────────────────────────────────
export function useToast() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: Toast['type'] = 'info') => {
    const id = Date.now().toString();
    setToasts(p => [...p, { id, message, type }]);
    setTimeout(() => setToasts(p => p.filter(t => t.id !== id)), 4000);
  }, []);

  const removeToast = useCallback(
    (id: string) => setToasts(p => p.filter(t => t.id !== id)),
    [],
  );

  return { toasts, addToast, removeToast };
}

// ── Renderer ──────────────────────────────────────────────────────────────────
export function ToastContainer({
  toasts,
  remove,
}: {
  toasts: Toast[];
  remove: (id: string) => void;
}) {
  return (
    <div className="fixed bottom-6 right-6 z-[100] flex flex-col gap-2 pointer-events-none">
      <AnimatePresence>
        {toasts.map(t => (
          <motion.div
            key={t.id}
            initial={{ opacity: 0, y: 20, scale: 0.9 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, scale: 0.9 }}
            className={cn(
              'flex items-center gap-3 px-4 py-3 rounded-xl shadow-2xl border text-sm font-medium pointer-events-auto max-w-sm',
              t.type === 'success' && 'bg-green-950/90 border-green-700 text-green-200',
              t.type === 'error'   && 'bg-red-950/90 border-red-700 text-red-200',
              t.type === 'info'    && 'bg-card border-border text-foreground',
            )}
          >
            {t.type === 'success' && <CheckCircle className="w-4 h-4 text-green-400 shrink-0" />}
            {t.type === 'error'   && <AlertCircle className="w-4 h-4 text-red-400 shrink-0" />}
            {t.type === 'info'    && <Clock className="w-4 h-4 text-primary shrink-0" />}
            <span className="flex-1">{t.message}</span>
            <button onClick={() => remove(t.id)} className="opacity-60 hover:opacity-100">
              <X className="w-3.5 h-3.5" />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
