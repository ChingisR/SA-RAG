import { motion } from 'framer-motion';
import { Upload } from 'lucide-react';

interface Props {
  filename: string;
  pct: number;
  status: string;
}

export function UploadProgress({ filename, pct, status }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className="fixed bottom-24 left-1/2 -translate-x-1/2 z-50 bg-card border border-border rounded-2xl p-4 shadow-2xl w-80"
    >
      <div className="flex items-center gap-3 mb-3">
        <Upload className="w-5 h-5 text-primary shrink-0" />
        <span className="text-sm font-semibold truncate flex-1">{filename}</span>
      </div>
      <div className="w-full bg-accent rounded-full h-1.5 overflow-hidden">
        <motion.div
          className="h-full bg-primary rounded-full"
          animate={{ width: `${pct}%` }}
          transition={{ ease: 'easeOut' }}
        />
      </div>
      <div className="text-[11px] text-muted-foreground mt-2 flex justify-between">
        <span>{status}</span>
        <span>{pct}%</span>
      </div>
    </motion.div>
  );
}
