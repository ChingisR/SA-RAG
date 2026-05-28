import { useEffect, useState, useRef } from 'react';
import { motion } from 'framer-motion';
import { FileText, X, ChevronLeft, ChevronRight } from 'lucide-react';

interface Highlight {
  left: number;
  top: number;
  width: number;
  height: number;
}

// Highly optimized sub-component to securely lazy-load PDF page images
function PdfPageImage({ 
  filename, 
  pageNum, 
  rootElement 
}: { 
  filename: string; 
  pageNum: number; 
  rootElement: HTMLDivElement | null;
}) {
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState<boolean>(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // 1. Observe visibility to trigger fetch only when close to viewport (400px margin)
  useEffect(() => {
    // If the scroll container is not available yet, fall back to the default viewport
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { 
        root: rootElement,
        rootMargin: '400px' 
      }
    );

    if (containerRef.current) {
      observer.observe(containerRef.current);
    }

    return () => observer.disconnect();
  }, [rootElement]);

  // 2. Fetch page image securely as a blob using Authorization headers
  useEffect(() => {
    if (!visible) return;

    let active = true;
    const token = localStorage.getItem('access_token');
    const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};

    fetch(`/api/files/${encodeURIComponent(filename)}/pages/${pageNum}/image`, { headers })
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.blob();
      })
      .then(blob => {
        if (active) {
          const url = URL.createObjectURL(blob);
          setImageUrl(url);
          setLoading(false);
        }
      })
      .catch(err => {
        if (active) {
          setError(err.message);
          setLoading(false);
        }
      });

    return () => {
      active = false;
      setImageUrl(prev => {
        if (prev) URL.revokeObjectURL(prev);
        return null;
      });
    };
  }, [filename, pageNum, visible]);

  return (
    <div ref={containerRef} className="w-full min-h-[400px] flex items-center justify-center">
      {error ? (
        <div className="text-red-400 text-xs py-4">Failed to load page: {error}</div>
      ) : loading ? (
        <div className="flex flex-col items-center justify-center py-20 space-y-2">
          <div className="w-6 h-6 rounded-full border-2 border-primary border-t-transparent animate-spin" />
          <span className="text-[10px] text-muted-foreground animate-pulse">Rendering page {pageNum}...</span>
        </div>
      ) : (
        <img 
          src={imageUrl || ''} 
          alt={`Page ${pageNum}`} 
          className="max-h-[85vh] w-auto h-auto block select-none shadow-sm animate-in fade-in duration-300"
          style={{ pointerEvents: 'none' }}
        />
      )}
    </div>
  );
}

export function PdfDrawer({ 
  filename, 
  page, 
  snippet, 
  onClose 
}: { 
  filename: string; 
  page: string; 
  snippet?: string; 
  onClose: () => void 
}) {
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [currentPage, setCurrentPage] = useState<number>(parseInt(page) || 1);
  const [highlights, setHighlights] = useState<Highlight[]>([]);
  const [hasTextLayer, setHasTextLayer] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  
  // Use state for the scroll container to ensure the update triggers re-renders and re-binds observers!
  const [scrollContainer, setScrollContainer] = useState<HTMLDivElement | null>(null);

  // 1. Fetch total page count and highlights on load
  useEffect(() => {
    setLoading(true);
    setError(null);
    const token = localStorage.getItem('access_token');
    const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};

    // Fetch page count
    fetch(`/api/files/${encodeURIComponent(filename)}/page-count`, { headers })
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status} while fetching page count.`);
        return res.json();
      })
      .then(data => {
        setPageCount(data.page_count);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });

    // Fetch highlights if snippet and page are provided
    if (snippet && page) {
      fetch(`/api/files/${encodeURIComponent(filename)}/pages/${page}/highlight?snippet=${encodeURIComponent(snippet)}`, { headers })
        .then(res => {
          if (res.ok) return res.json();
          return null;
        })
        .then(data => {
          if (data) {
            setHighlights(data.highlights || []);
            setHasTextLayer(data.has_text_layer ?? true);
          }
        })
        .catch(err => console.error("Failed to load highlights:", err));
    } else {
      setHighlights([]);
      setHasTextLayer(true);
    }
  }, [filename, page, snippet]);

  // 2. IntersectionObserver to update currentPage dynamically as user scrolls
  useEffect(() => {
    if (!pageCount || !scrollContainer) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const pageNum = parseInt(entry.target.getAttribute('data-page') || '1');
            setCurrentPage(pageNum);
          }
        });
      },
      {
        root: scrollContainer,
        threshold: 0.4, // Trigger when 40% of the page is visible
      }
    );

    // Give the DOM a moment to render the page cards
    const timeoutId = setTimeout(() => {
      for (let i = 1; i <= pageCount; i++) {
        const el = document.getElementById(`page-card-${i}`);
        if (el) observer.observe(el);
      }
    }, 100);

    return () => {
      observer.disconnect();
      clearTimeout(timeoutId);
    };
  }, [pageCount, scrollContainer]);

  // 3. Scroll to the initially selected page when pageCount is first loaded
  useEffect(() => {
    if (pageCount && page && scrollContainer) {
      const initialPageNum = parseInt(page);
      if (initialPageNum > 0 && initialPageNum <= pageCount) {
        setTimeout(() => {
          const el = document.getElementById(`page-card-${initialPageNum}`);
          if (el) {
            el.scrollIntoView({ behavior: 'auto', block: 'start' });
            setCurrentPage(initialPageNum);
          }
        }, 150);
      }
    }
  }, [pageCount, page, scrollContainer]);

  // Handle manual page navigation (Prev/Next buttons)
  const navigateToPage = (targetPage: number) => {
    if (!pageCount || targetPage < 1 || targetPage > pageCount) return;
    const el = document.getElementById(`page-card-${targetPage}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setCurrentPage(targetPage);
    }
  };

  return (
    <div className="absolute inset-0 z-50 bg-background/80 backdrop-blur-sm flex justify-end">
      <div className="w-full max-w-4xl bg-card border-l border-border flex flex-col shadow-2xl animate-in slide-in-from-right duration-300">
        
        {/* Header */}
        <div className="p-4 border-b border-border flex items-center justify-between bg-card/95 backdrop-blur z-10">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <FileText className="w-6 h-6 text-primary shrink-0" />
            <span className="font-bold truncate text-foreground text-sm md:text-base">{filename}</span>
          </div>

          <div className="flex items-center gap-4 shrink-0 pl-4">
            {pageCount && (
              <div className="flex items-center gap-2 bg-accent/50 px-3 py-1 rounded-lg border border-border">
                <button 
                  onClick={() => navigateToPage(currentPage - 1)}
                  disabled={currentPage <= 1}
                  className="p-1 hover:bg-accent rounded transition-colors disabled:opacity-30 disabled:pointer-events-none"
                  title="Previous Page"
                >
                  <ChevronLeft className="w-4 h-4 text-foreground" />
                </button>
                <span className="text-xs font-semibold text-foreground select-none">
                  Page {currentPage} of {pageCount}
                </span>
                <button 
                  onClick={() => navigateToPage(currentPage + 1)}
                  disabled={currentPage >= pageCount}
                  className="p-1 hover:bg-accent rounded transition-colors disabled:opacity-30 disabled:pointer-events-none"
                  title="Next Page"
                >
                  <ChevronRight className="w-4 h-4 text-foreground" />
                </button>
              </div>
            )}

            {!hasTextLayer && (
              <span className="hidden md:inline-block text-[10px] bg-amber-500/10 border border-amber-500/30 text-amber-400 px-2 py-1 rounded font-semibold animate-pulse">
                Scanned Image
              </span>
            )}

            <button onClick={onClose} className="p-2 hover:bg-accent rounded-lg transition-colors" title="Close Panel">
              <X className="w-5 h-5 text-muted-foreground" />
            </button>
          </div>
        </div>

        {/* Scrollable Container with all pages stack */}
        <div 
          ref={setScrollContainer}
          id="pages-scroll-container"
          className="flex-1 bg-accent/10 p-4 md:p-8 overflow-y-auto space-y-8 flex flex-col items-center"
        >
          {error ? (
            <div className="flex items-center justify-center h-full w-full text-red-400 text-sm">
              Failed to load PDF metadata: {error}
            </div>
          ) : loading ? (
            <div className="flex flex-col items-center justify-center h-full w-full space-y-4 py-20">
              <div className="w-8 h-8 rounded-full border-2 border-primary border-t-transparent animate-spin" />
              <div className="text-muted-foreground text-sm animate-pulse">
                Analyzing PDF structure and pages…
              </div>
            </div>
          ) : pageCount && pageCount > 0 ? (
            Array.from({ length: pageCount }).map((_, idx) => {
              const pageNum = idx + 1;
              const isTargetPage = pageNum === parseInt(page);
              
              return (
                <div 
                  key={pageNum}
                  id={`page-card-${pageNum}`}
                  data-page={pageNum}
                  className="relative flex flex-col items-center w-full max-w-2xl bg-card rounded-xl border border-border shadow-md overflow-hidden transition-all duration-300 hover:shadow-xl scroll-mt-4 shrink-0"
                >
                  {/* Page Divider / Label */}
                  <div className="w-full bg-accent/30 px-4 py-2 border-b border-border flex items-center justify-between text-xs font-semibold text-muted-foreground select-none">
                    <span>PAGE {pageNum}</span>
                    {isTargetPage && snippet && (
                      <span className="text-[10px] bg-primary/20 text-primary border border-primary/30 px-2 py-0.5 rounded-full font-bold">
                        Source Citation Location
                      </span>
                    )}
                  </div>

                  {/* Page Image Container */}
                  <div className="relative p-2 bg-white flex justify-center items-center w-full">
                    {/* Lazy-loaded secure image component with state-based scroll root */}
                    <PdfPageImage filename={filename} pageNum={pageNum} rootElement={scrollContainer} />

                    {/* Highlights overlay (Only on the specific citation page) */}
                    {isTargetPage && highlights.map((h, i) => (
                      <motion.div
                        key={i}
                        initial={{ opacity: 0, scale: 0.98 }}
                        animate={{ 
                          opacity: [0.35, 0.65, 0.35], 
                          scale: [1, 1.008, 1] 
                        }}
                        transition={{ 
                          opacity: { repeat: Infinity, duration: 2.5, ease: "easeInOut" },
                          scale: { repeat: Infinity, duration: 2.5, ease: "easeInOut" }
                        }}
                        className="absolute border-2 border-[#005baa] bg-[#005baa]/20 shadow-[0_0_12px_rgba(0,91,170,0.4)] rounded-sm pointer-events-none transition-all duration-300"
                        style={{
                          left: `${h.left}%`,
                          top: `${h.top}%`,
                          width: `${h.width}%`,
                          height: `${h.height}%`,
                        }}
                        title="Source citation evidence"
                      />
                    ))}
                  </div>
                </div>
              );
            })
          ) : (
            <div className="text-muted-foreground text-sm py-20">No pages found in this document.</div>
          )}
        </div>
      </div>
    </div>
  );
}
