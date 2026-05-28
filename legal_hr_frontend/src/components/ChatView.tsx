import { useRef, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Upload, Send, Mic, User, Shield, FileText, ThumbsUp, ThumbsDown,
  MessageSquare, BrainCircuit, ChevronDown, ChevronUp,
  Search, Database, Network, Eye, Globe, CheckCircle2, Activity, Play
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { useTranslation } from 'react-i18next';
import type { ChatMessage, QuerySettings } from '../api';
import { uploadPdf, streamQuery, transcribeAudio, createSession, getTaskStatus, submitFeedback } from '../api';
import { cn } from '../lib/cn';

// ── Smart Table Visualization ─────────────────────────────────────────────────
import {
  BarChart as RechartsBarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer
} from 'recharts';

function SmartMarkdownRenderer({ content }: { content: string }) {
  // Matches standard markdown tables
  const tableRegex = /(\|.*\|[\r\n]+\|[-:| ]+\|[\r\n]+((?:\|.*\|[\r\n]*)+))/;
  const match = content.match(tableRegex);

  let chartComponent = null;

  if (match) {
    const rawTable = match[0].trim();
    const lines = rawTable.split('\n');
    const headers = lines[0].split('|').map(s => s.trim()).filter(Boolean);
    const dataRecords = [];
    let hasNumbers = false;

    // Start parsing after header and divider
    for (let i = 2; i < lines.length; i++) {
      const cells = lines[i].split('|').map(s => s.trim()).filter(Boolean);
      if (cells.length >= 2) {
        // Strip commas/formatting for numeric check
        const num = parseFloat(cells[1].replace(/,/g, '').replace(/\$/g, ''));
        if (!isNaN(num)) hasNumbers = true;
        dataRecords.push({
          name: cells[0].substring(0, 15), // Trucate long X-axis labels
          value: !isNaN(num) ? num : 0
        });
      }
    }

    if (dataRecords.length > 0 && hasNumbers && headers.length >= 2) {
      chartComponent = (
        <div className="bg-background/50 border border-border/50 p-4 rounded-xl mt-4 h-64 w-full">
          <h4 className="text-[10px] font-bold text-muted-foreground mb-4 uppercase tracking-widest">{headers[0]} VS {headers[1]}</h4>
          <ResponsiveContainer width="100%" height="80%">
            <RechartsBarChart data={dataRecords} margin={{ left: -20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
              <XAxis dataKey="name" stroke="#888" tick={{ fontSize: 9 }} />
              <YAxis stroke="#888" tick={{ fontSize: 9 }} />
              <Tooltip contentStyle={{ backgroundColor: '#1e1e1e', borderColor: '#333', fontSize: '11px', borderRadius: '8px' }} cursor={{ fill: '#ffffff10' }} />
              <Bar dataKey="value" fill="#005baa" radius={[4, 4, 0, 0]} />
            </RechartsBarChart>
          </ResponsiveContainer>
        </div>
      );
    }
  }

  return (
    <div className="markdown-body prose prose-sm prose-invert max-w-none">
      <ReactMarkdown>{content}</ReactMarkdown>
      {chartComponent}
    </div>
  );
}

// ── Thinking Block Helper & Metadata ──────────────────────────────────────────
interface ThoughtOrTool {
  type: 'think' | 'tool';
  text: string;
  toolName?: string;
  query?: string;
  isCompleted: boolean;
}

function parseThinkingContent(content: string): ThoughtOrTool[] {
  const items: ThoughtOrTool[] = [];
  const regex = /(<think>[\s\S]*?<\/think>|<tool_call>[\s\S]*?(?:<\/function>|<\/tool_call>)|<tool_call>[\s\S]*$)/g;
  
  let match;
  while ((match = regex.exec(content)) !== null) {
    const matchedText = match[0];
    
    if (matchedText.startsWith('<think>')) {
      const thinkText = matchedText.replace(/<\/?think>/g, '').trim();
      if (thinkText) {
        items.push({ 
          type: 'think', 
          text: thinkText, 
          isCompleted: !matchedText.endsWith('<think>') && matchedText.includes('</think>') 
        });
      }
    } else if (matchedText.startsWith('<tool_call>')) {
      const funcMatch = matchedText.match(/function=(\w+)/);
      const funcName = funcMatch ? funcMatch[1] : 'unknown_tool';
      
      const paramMatch = matchedText.match(/<parameter=[^>]+>([\s\S]*?)<\/parameter>/);
      const query = paramMatch ? paramMatch[1].trim() : '';
      
      const isCompleted = matchedText.includes('</function>') || matchedText.includes('</tool_call>');
      
      items.push({
        type: 'tool',
        text: matchedText,
        toolName: funcName,
        query: query,
        isCompleted
      });
    }
  }
  
  if (items.length === 0 && content.trim()) {
    items.push({ type: 'think', text: content.trim(), isCompleted: true });
  }

  return items;
}

const TOOL_METADATA: Record<string, { label: string; icon: any; colorClass: string; bgClass: string }> = {
  document_agent_tool: {
    label: "Document Library Search",
    icon: Search,
    colorClass: "text-blue-400 border-blue-500/30",
    bgClass: "bg-blue-500/5 hover:bg-blue-500/10"
  },
  sql_agent_tool: {
    label: "Corporate SQL Database",
    icon: Database,
    colorClass: "text-purple-400 border-purple-500/30",
    bgClass: "bg-purple-500/5 hover:bg-purple-500/10"
  },
  graph_agent_tool: {
    label: "Neo4j Knowledge Graph Analysis",
    icon: Network,
    colorClass: "text-emerald-400 border-emerald-500/30",
    bgClass: "bg-emerald-500/5 hover:bg-emerald-500/10"
  },
  vision_agent_tool: {
    label: "Chart & Image Vision Reader",
    icon: Eye,
    colorClass: "text-amber-400 border-amber-500/30",
    bgClass: "bg-amber-500/5 hover:bg-amber-500/10"
  },
  web_search_tool: {
    label: "Public Web Search",
    icon: Globe,
    colorClass: "text-cyan-400 border-cyan-500/30",
    bgClass: "bg-cyan-500/5 hover:bg-cyan-500/10"
  },
  unknown_tool: {
    label: "Reasoning Engine Agent",
    icon: BrainCircuit,
    colorClass: "text-muted-foreground border-border",
    bgClass: "bg-muted/5 hover:bg-muted/10"
  }
};

// ── Thinking Block ────────────────────────────────────────────────────────────
function ThinkingBlock({ content }: { content: string }) {
  const [isOpen, setIsOpen] = useState(false);
  const steps = parseThinkingContent(content);
  
  const completedCount = steps.filter(s => s.isCompleted).length;
  const totalCount = steps.length;
  const isRunning = steps.some(s => !s.isCompleted);

  return (
    <div className="mb-4 border border-border/40 rounded-2xl overflow-hidden bg-accent/5 backdrop-blur-md transition-all duration-300 hover:shadow-lg hover:shadow-primary/5">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between p-4 text-xs font-semibold text-muted-foreground hover:text-foreground transition-all duration-300"
      >
        <span className="flex items-center gap-2">
          {isRunning ? (
            <Activity className="w-4 h-4 text-primary animate-pulse" />
          ) : (
            <BrainCircuit className="w-4 h-4 text-emerald-500" />
          )}
          <span>Agent Reasoning &amp; Tool Calls</span>
          <span className="ml-2 px-2 py-0.5 rounded-full text-[10px] bg-accent/25 border border-border/30">
            {completedCount}/{totalCount} Steps
          </span>
        </span>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground/60 font-normal">
            {isRunning ? "Running..." : "Completed"}
          </span>
          {isOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </div>
      </button>
      
      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            className="px-4 pb-4 border-t border-border/40"
          >
            <div className="pt-4 space-y-3">
              {steps.map((step, idx) => {
                if (step.type === 'think') {
                  return (
                    <div key={idx} className="flex gap-3 text-xs leading-relaxed text-muted-foreground pl-1">
                      <div className="flex flex-col items-center">
                        <div className="w-5 h-5 rounded-full bg-accent/40 border border-border flex items-center justify-center flex-shrink-0">
                          <BrainCircuit className="w-3 h-3 text-muted-foreground" />
                        </div>
                        {idx < steps.length - 1 && <div className="w-[1px] bg-border/40 flex-grow my-1" />}
                      </div>
                      <div className="flex-1 pt-0.5 whitespace-pre-wrap italic text-muted-foreground/90">
                        {step.text}
                      </div>
                    </div>
                  );
                }

                const meta = TOOL_METADATA[step.toolName || 'unknown_tool'] || TOOL_METADATA.unknown_tool;
                const Icon = meta.icon;

                return (
                  <div key={idx} className="flex gap-3 text-xs leading-relaxed pl-1">
                    <div className="flex flex-col items-center">
                      <div className={cn(
                        "w-5 h-5 rounded-full border flex items-center justify-center flex-shrink-0",
                        step.isCompleted ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400" : "bg-primary/10 border-primary/30 text-primary animate-pulse"
                      )}>
                        {step.isCompleted ? (
                          <CheckCircle2 className="w-3 h-3" />
                        ) : (
                          <Play className="w-2.5 h-2.5 fill-current" />
                        )}
                      </div>
                      {idx < steps.length - 1 && <div className="w-[1px] bg-border/40 flex-grow my-1" />}
                    </div>
                    
                    <div className="flex-1 space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-foreground/90">Executed: {meta.label}</span>
                        <span className={cn(
                          "px-2 py-0.5 rounded-md text-[9px] border font-medium flex items-center gap-1",
                          meta.colorClass,
                          meta.bgClass
                        )}>
                          <Icon className="w-2.5 h-2.5" />
                          {step.toolName}
                        </span>
                      </div>
                      {step.query && (
                        <div className="bg-accent/15 border border-border/30 rounded-lg p-2.5 font-mono text-[10px] text-muted-foreground/90 break-all select-all">
                          <span className="text-primary/70 font-semibold mr-1">Query:</span>
                          {step.query}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Message Formatter ─────────────────────────────────────────────────────────
function formatMessage(content: string, isCurrentlyStreaming?: boolean) {
  if (!content) {
    if (isCurrentlyStreaming) {
      return (
        <div className="space-y-2">
          <ThinkingBlock content="Initializing thought process..." />
        </div>
      );
    }
    return null;
  }

  let thinkContent = '';
  let mainContent = content;

  // 1. Extract <think> block
  const thinkStart = mainContent.indexOf('<think>');
  if (thinkStart !== -1) {
    const thinkEnd = mainContent.lastIndexOf('</think>');
    if (thinkEnd !== -1 && thinkStart < thinkEnd) {
      thinkContent += mainContent.substring(thinkStart + 7, thinkEnd).trim() + '\n\n';
      mainContent = mainContent.substring(0, thinkStart) + mainContent.substring(thinkEnd + 8);
    } else {
      thinkContent += mainContent.substring(thinkStart + 7).trim() + '\n\n';
      mainContent = mainContent.substring(0, thinkStart);
    }
  }

  // 2. Extract multiple completed tool calls matching <tool_call>... </function> or </tool_call>
  const toolCallRegex = /<tool_call>[\s\S]*?(?:<\/function>|<\/tool_call>)/g;
  const toolCalls = mainContent.match(toolCallRegex);

  if (toolCalls) {
    toolCalls.forEach(call => {
      thinkContent += call.trim() + '\n\n';
    });
    mainContent = mainContent.replace(toolCallRegex, '').trim();
  }

  // 3. Extract any remaining unclosed/currently-streaming tool call
  const openToolIndex = mainContent.indexOf('<tool_call>');
  if (openToolIndex !== -1) {
    thinkContent += mainContent.substring(openToolIndex).trim() + '\n\n';
    mainContent = mainContent.substring(0, openToolIndex).trim();
  }

  mainContent = mainContent.trim();
  thinkContent = thinkContent.trim();

  // If there's no thinkContent yet, but we are streaming, show the initializing state
  if (!thinkContent && !mainContent && isCurrentlyStreaming) {
    return (
      <div className="space-y-2">
        <ThinkingBlock content="Initializing thought process..." />
      </div>
    );
  }

  if (thinkContent) {
    return (
      <div className="space-y-2">
        <ThinkingBlock content={thinkContent} />
        {mainContent && <SmartMarkdownRenderer content={mainContent} />}
      </div>
    );
  }

  return <SmartMarkdownRenderer content={mainContent || content} />;
}

// ── ChatView Props ────────────────────────────────────────────────────────────
interface Props {
  messages: ChatMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  sessions: any[];
  setSessions: React.Dispatch<React.SetStateAction<any[]>>;
  currentSessionId: string | null;
  setCurrentSessionId: (id: string | null) => void;
  isStreaming: boolean;
  setIsStreaming: (v: boolean) => void;
  settings: QuerySettings;
  onToast: (msg: string, type: 'success' | 'error' | 'info') => void;
  onUploadProgress: (state: { filename: string; pct: number; status: string } | null) => void;
  onSelectDocument: (doc: { filename: string; page: string; snippet?: string }) => void;
}

// ── ChatView ──────────────────────────────────────────────────────────────────
export function ChatView({
  messages,
  setMessages,
  sessions: _sessions,
  setSessions,
  currentSessionId,
  setCurrentSessionId,
  isStreaming,
  setIsStreaming,
  settings,
  onToast,
  onUploadProgress,
  onSelectDocument,
}: Props) {
  const { t } = useTranslation();
  const [input, setInput] = useState('');
  const [feedbackSent, setFeedbackSent] = useState<Record<string, 1 | -1>>({});
  const [isRecording, setIsRecording] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);

  // Auto-scroll
  const scrollToBottom = () => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });

  // ── Send Message ──────────────────────────────────────────────────────────
  const sendMessage = async () => {
    if (!input.trim() || isStreaming) return;
    const userMsg: ChatMessage = { id: Date.now().toString(), role: 'user', content: input };
    const snapshot = [...messages, userMsg];
    setMessages(snapshot);
    setInput('');
    setIsStreaming(true);
    setTimeout(scrollToBottom, 50);

    let activeSessionId = currentSessionId;
    if (!activeSessionId) {
      try {
        const title = input.trim().substring(0, 30) + (input.length > 30 ? '...' : '');
        const sess = await createSession(title);
        activeSessionId = sess.session_id;
        setCurrentSessionId(activeSessionId);
        setSessions(prev => [sess, ...prev]);
      } catch (err) {
        console.error('Session creation error', err);
      }
    }

    const assistantId = (Date.now() + 1).toString();
    setMessages(prev => [...prev, { id: assistantId, role: 'assistant', content: '' }]);

    try {
      await streamQuery(
        input,
        messages,
        settings,
        chunk => {
          setMessages(prev =>
            prev.map(m => m.id === assistantId ? { ...m, content: m.content + chunk } : m),
          );
          scrollToBottom();
        },
        citations =>
          setMessages(prev =>
            prev.map(m => m.id === assistantId ? { ...m, sources: citations } : m),
          ),
        activeSessionId || undefined,
      );
    } catch (err) {
      console.error(err);
    } finally {
      setIsStreaming(false);
    }
  };

  // ── Feedback ──────────────────────────────────────────────────────────────
  const handleFeedback = async (msgId: string, rating: 1 | -1) => {
    try {
      await submitFeedback(msgId, rating);
      setFeedbackSent(prev => ({ ...prev, [msgId]: rating }));
      onToast(
        rating === 1 ? 'Thanks for the feedback! 👍' : "Feedback noted — we'll improve.",
        'success',
      );
    } catch {
      onToast('Could not save feedback.', 'error');
    }
  };

  // ── File Upload ───────────────────────────────────────────────────────────
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = '';
    onUploadProgress({ filename: file.name, pct: 0, status: 'Uploading…' });
    try {
      const res = await uploadPdf(file, pct =>
        onUploadProgress({ filename: file.name, pct, status: 'Uploading…' }),
      );
      onUploadProgress({ filename: file.name, pct: 100, status: 'Processing in background…' });
      onToast(`${file.name} uploaded. Processing…`, 'info');

      if (res.task_id) {
        let attempts = 0;
        const poll = setInterval(async () => {
          attempts++;
          try {
            const st = await getTaskStatus(res.task_id);
            if (st.status === 'success') {
              clearInterval(poll);
              onUploadProgress(null);
              onToast(`${file.name} indexed successfully!`, 'success');
            } else if (st.status === 'failure') {
              clearInterval(poll);
              onUploadProgress(null);
              onToast(`Indexing failed: ${st.detail}`, 'error');
            } else if (attempts > 60) {
              clearInterval(poll);
              onUploadProgress(null);
            }
          } catch {
            clearInterval(poll);
            onUploadProgress(null);
          }
        }, 5000);
      } else {
        setTimeout(() => onUploadProgress(null), 1500);
      }

      setMessages(prev => [
        ...prev,
        { id: Date.now().toString(), role: 'assistant', content: `*${file.name}* ${t('chat.upload_async')}` },
      ]);
    } catch (err: any) {
      onUploadProgress(null);
      onToast(
        `Upload failed: ${err?.response?.data?.detail || err?.message || 'Unknown error'}`,
        'error',
      );
    }
  };

  // ── Voice Recording ───────────────────────────────────────────────────────
  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      mediaRecorderRef.current = mr;
      audioChunksRef.current = [];
      mr.ondataavailable = e => { if (e.data.size > 0) audioChunksRef.current.push(e.data); };
      mr.onstop = async () => {
        const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        try {
          const res = await transcribeAudio(blob);
          if (res.text) setInput(p => p ? p + ' ' + res.text : res.text);
        } catch (e) { console.error('Transcription error:', e); }
        stream.getTracks().forEach(t => t.stop());
      };
      mr.start();
      setIsRecording(true);
    } catch (e) { console.error('Mic error:', e); }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && isRecording) {
      mediaRecorderRef.current.stop();
      setIsRecording(false);
    }
  };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <>
      {/* Message List */}
      <div className="flex-1 overflow-y-auto p-6 md:p-12 space-y-8 scroll-smooth">
        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-center space-y-6 opacity-30 select-none">
            <MessageSquare className="w-24 h-24 mb-4" />
            <h2 className="text-2xl font-bold">{t('welcome.title')}</h2>
            <p className="max-w-md">{t('welcome.subtitle')}</p>
          </div>
        )}

        {messages.map((msg, index) => (
          <div key={msg.id} className={cn('flex gap-4 group', msg.role === 'user' ? 'flex-row-reverse' : 'flex-row')}>
            {/* Avatar */}
            <div className={cn(
              'w-10 h-10 rounded-xl flex items-center justify-center shrink-0',
              msg.role === 'user'
                ? 'bg-primary text-primary-foreground'
                : 'bg-card border border-border text-muted-foreground',
            )}>
              {msg.role === 'user' ? <User className="w-6 h-6" /> : <Shield className="w-6 h-6" />}
            </div>

            {/* Bubble */}
            <div className={cn('max-w-[80%] space-y-2', msg.role === 'user' && 'text-right')}>
              <div className={cn(
                'p-4 rounded-2xl text-sm leading-relaxed shadow-sm overflow-hidden break-words',
                msg.role === 'user'
                  ? 'bg-primary/95 text-primary-foreground rounded-tr-none whitespace-pre-wrap'
                  : 'bg-card border border-border rounded-tl-none',
              )}>
                {msg.role === 'user' ? msg.content : formatMessage(msg.content, isStreaming && index === messages.length - 1)}
              </div>

              {/* Citations */}
              {msg.sources && msg.sources.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {msg.sources.map((src, i) => (
                    <button
                      key={i}
                      onClick={() => onSelectDocument({ filename: src.filename, page: src.page, snippet: src.snippet })}
                      className="flex items-center gap-2 px-3 py-1.5 bg-accent hover:bg-primary/20 text-[11px] font-bold rounded-lg border border-border transition-all"
                    >
                      <FileText className="w-3.5 h-3.5" />
                      {src.filename} (P{src.page})
                    </button>
                  ))}
                </div>
              )}

              {/* Feedback */}
              {msg.role === 'assistant' && msg.content && !isStreaming && (
                <div className="flex items-center gap-1 mt-1">
                  {feedbackSent[msg.id] === undefined ? (
                    <>
                      <button
                        onClick={() => handleFeedback(msg.id, 1)}
                        className="p-1.5 rounded-lg text-muted-foreground hover:text-green-400 hover:bg-green-400/10 transition-colors"
                        title="Good response"
                      >
                        <ThumbsUp className="w-3.5 h-3.5" />
                      </button>
                      <button
                        onClick={() => handleFeedback(msg.id, -1)}
                        className="p-1.5 rounded-lg text-muted-foreground hover:text-red-400 hover:bg-red-400/10 transition-colors"
                        title="Poor response"
                      >
                        <ThumbsDown className="w-3.5 h-3.5" />
                      </button>
                    </>
                  ) : (
                    <span className={cn(
                      'text-[11px] font-medium flex items-center gap-1',
                      feedbackSent[msg.id] === 1 ? 'text-green-400' : 'text-red-400',
                    )}>
                      {feedbackSent[msg.id] === 1
                        ? <ThumbsUp className="w-3 h-3" />
                        : <ThumbsDown className="w-3 h-3" />
                      }
                      Recorded
                    </span>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}

        {/* Streaming skeleton */}
        {isStreaming && (
          <div className="flex gap-4 animate-pulse">
            <div className="w-10 h-10 rounded-xl bg-card border border-border shrink-0" />
            <div className="bg-card border border-border h-12 w-32 rounded-2xl p-4" />
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Bar */}
      <div className="p-6 border-t border-border bg-card/80 backdrop-blur-xl">
        <div className="max-w-4xl mx-auto flex items-end gap-3 bg-accent rounded-2xl p-3 border border-border/50 focus-within:ring-2 focus-within:ring-primary/20 transition-all">
          <label
            className="p-2 text-muted-foreground hover:text-primary transition-colors cursor-pointer"
            title="Upload document"
          >
            <Upload className="w-6 h-6" />
            <input
              type="file"
              className="hidden"
              accept=".pdf,.docx,.xlsx,.xls,.pptx,.txt,.md,.csv,.jpg,.jpeg,.png"
              onChange={handleFileUpload}
            />
          </label>

          <button
            type="button"
            onMouseDown={startRecording}
            onMouseUp={stopRecording}
            onMouseLeave={stopRecording}
            onTouchStart={startRecording}
            onTouchEnd={stopRecording}
            className={cn(
              'p-2 transition-colors rounded-full',
              isRecording ? 'text-red-500 bg-red-500/10 animate-pulse' : 'text-muted-foreground hover:text-primary',
            )}
          >
            <Mic className="w-6 h-6" />
          </button>

          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
            placeholder={t('chat.input_placeholder')}
            rows={1}
            className="flex-1 bg-transparent border-none outline-none resize-none py-2 text-sm max-h-48"
          />

          <button
            onClick={sendMessage}
            disabled={isStreaming || !input.trim()}
            className="p-2 bg-primary text-primary-foreground rounded-xl disabled:opacity-30 transition-all shadow-lg shadow-primary/20"
          >
            <Send className="w-6 h-6" />
          </button>
        </div>
      </div>

    </>
  );
}
