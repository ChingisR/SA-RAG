import { useCallback, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { FolderOpen, FileText, Trash2, RefreshCw, Eye } from 'lucide-react';
import type { IngestedDocument } from '../api';
import { listDocuments, deleteDocument } from '../api';
import { cn } from '../lib/cn';

interface Props {
  onToast: (msg: string, type: 'success' | 'error' | 'info') => void;
  onSelectDocument: (doc: { filename: string; page: string; snippet?: string }) => void;
}

export function DocumentManager({ onToast, onSelectDocument }: Props) {
  const [docs, setDocs] = useState<IngestedDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState<string | null>(null);

  const fetchDocs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listDocuments();
      setDocs(res.documents);
    } catch {
      onToast('Failed to load documents', 'error');
    } finally {
      setLoading(false);
    }
  }, [onToast]);

  useEffect(() => { fetchDocs(); }, [fetchDocs]);

  const handleDelete = async (filename: string) => {
    if (!confirm(`Delete all chunks of "${filename}" from the vector store?`)) return;
    setDeleting(filename);
    try {
      const res = await deleteDocument(filename);
      onToast(`Deleted ${res.deleted_chunks} chunks of "${filename}"`, 'success');
      setDocs(prev => prev.filter(d => d.filename !== filename));
    } catch {
      onToast(`Failed to delete "${filename}"`, 'error');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-10 space-y-6 bg-background">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold flex items-center gap-2">
          <FolderOpen className="w-6 h-6 text-primary" /> Document Library
        </h2>
        <button
          onClick={fetchDocs}
          className="p-2 hover:bg-accent rounded-lg transition-colors text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <RefreshCw className="w-8 h-8 text-primary animate-spin" />
        </div>
      ) : docs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center text-muted-foreground space-y-4">
          <FolderOpen className="w-16 h-16 opacity-30" />
          <p className="text-lg font-medium">No documents indexed yet.</p>
          <p className="text-sm">Upload a file using the chat input to get started.</p>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {docs.length} document{docs.length !== 1 ? 's' : ''} indexed
          </p>
          {docs.map(doc => (
            <motion.div
              key={doc.filename}
              layout
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95 }}
              onClick={() => onSelectDocument({ filename: doc.filename, page: '1' })}
              className="bg-card border border-border rounded-xl p-4 flex items-start gap-4 hover:border-primary/40 hover:bg-accent/20 cursor-pointer active:scale-[0.995] transition-all group relative overflow-hidden"
            >
              <FileText className="w-8 h-8 text-primary shrink-0 mt-0.5 transition-transform group-hover:scale-110 duration-300" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-2">
                  <p className="font-semibold text-sm truncate group-hover:text-primary transition-colors duration-300">{doc.filename}</p>
                  <span className="opacity-0 group-hover:opacity-100 transition-opacity duration-300 text-xs text-primary font-bold flex items-center gap-1 shrink-0 bg-primary/10 px-2.5 py-1 rounded-full border border-primary/20">
                    <Eye className="w-3 h-3" /> View
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-1 flex-wrap">
                  <span className="text-xs bg-primary/10 text-primary px-2 py-0.5 rounded font-medium">
                    {doc.document_type}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {doc.chunks} chunk{doc.chunks !== 1 ? 's' : ''}
                  </span>
                </div>
                {doc.summary && (
                  <p className="text-xs text-muted-foreground mt-1.5 line-clamp-2">{doc.summary}</p>
                )}
              </div>
              {/* Deletion disabled and hidden by user request (references kept to avoid TS6133 unused error) */}
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(doc.filename); }}
                disabled={true}
                className="hidden"
              >
                {deleting === doc.filename
                  ? <RefreshCw className="w-4 h-4 animate-spin" />
                  : <Trash2 className="w-4 h-4" />
                }
              </button>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}
