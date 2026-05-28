import { useState, useEffect, Suspense, lazy } from 'react';
import { AnimatePresence } from 'framer-motion';

import type { ChatMessage, QuerySettings, ChatSession } from './api';
import { login, getSessions, getSessionMessages, deleteSession, getUserSettings } from './api';

import { ErrorBoundary } from './components/ErrorBoundary';
import { ToastContainer, useToast } from './components/ToastContainer';
import { UploadProgress } from './components/UploadProgress';
import { LoginPage } from './components/LoginPage';
import { Sidebar } from './components/Sidebar';

// Lazy load heavy views
const ChatView = lazy(() => import('./components/ChatView').then(m => ({ default: m.ChatView })));
const SettingsPanel = lazy(() => import('./components/SettingsPanel').then(m => ({ default: m.SettingsPanel })));
const AnalyticsDashboard = lazy(() => import('./components/AnalyticsDashboard').then(m => ({ default: m.AnalyticsDashboard })));
const DocumentManager = lazy(() => import('./components/DocumentManager').then(m => ({ default: m.DocumentManager })));

import { PdfDrawer } from './components/PdfDrawer';
import './App.css';

type ActiveView = 'chat' | 'settings' | 'analytics' | 'documents';

export default function App() {
  // ── Auth ────────────────────────────────────────────────────────────────
  const [isLoggedIn, setIsLoggedIn] = useState(false);
  const [userRole, setUserRole] = useState<string>('HR_Admin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');

  // ── Chat state ───────────────────────────────────────────────────────────
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [activeView, setActiveView] = useState<ActiveView>('chat');

  // ── UI state ─────────────────────────────────────────────────────────────
  const { toasts, addToast, removeToast } = useToast();
  const [uploadState, setUploadState] = useState<{ filename: string; pct: number; status: string } | null>(null);
  const [selectedPdf, setSelectedPdf] = useState<{ filename: string; page: string; snippet?: string } | null>(null);

  // ── Settings ─────────────────────────────────────────────────────────────
  const [settings, setSettings] = useState<QuerySettings>({
    similarity_top_k: 20,
    rerank_top_n: 5,
    temperature: 0.1,
    user_role: 'HR_Admin',
    framework: 'langgraph',
    output_thinking: false,
  });

  // Sync settings.user_role whenever userRole changes
  useEffect(() => {
    setSettings(p => ({ ...p, user_role: userRole }));
  }, [userRole]);

  // Load sessions and settings on login
  useEffect(() => {
    if (isLoggedIn) {
      getSessions().then(setSessions).catch(console.error);
      getUserSettings().then(saved => {
        setSettings(p => ({
          ...p,
          framework: saved.preferred_agent,
          output_thinking: saved.output_thinking ?? false,
        }));
      }).catch(console.error);
    }
  }, [isLoggedIn]);

  // ── Handlers ─────────────────────────────────────────────────────────────
  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError('');
    try {
      const data = await login(email, password);
      localStorage.setItem('access_token', data.access_token);
      setUserRole(data.user.role);
      setIsLoggedIn(true);
    } catch (err: any) {
      setLoginError(err.response?.data?.detail || 'Login failed. Check credentials.');
    }
  };

  const handleLogout = () => {
    setIsLoggedIn(false);
    localStorage.removeItem('access_token');
  };

  const handleLoadSession = async (sessionId: string) => {
    try {
      setCurrentSessionId(sessionId);
      const msgs = await getSessionMessages(sessionId);
      setMessages(msgs);
      setActiveView('chat');
    } catch (e) { console.error(e); }
  };

  const handleDeleteSession = async (sessionId: string) => {
    await deleteSession(sessionId);
    setSessions(prev => prev.filter(x => x.session_id !== sessionId));
    if (currentSessionId === sessionId) {
      setCurrentSessionId(null);
      setMessages([]);
      setActiveView('chat');
    }
  };

  const handleNewChat = () => {
    setCurrentSessionId(null);
    setMessages([]);
    setActiveView('chat');
  };

  const isAdmin = userRole === 'HR_Admin' || userRole === 'admin';

  // ── Login gate ────────────────────────────────────────────────────────────
  if (!isLoggedIn) {
    return (
      <LoginPage
        email={email}
        setEmail={setEmail}
        password={password}
        setPassword={setPassword}
        error={loginError}
        onSubmit={handleLogin}
      />
    );
  }

  // ── Main Layout ───────────────────────────────────────────────────────────
  return (
    <div className="flex h-screen bg-background text-foreground overflow-hidden">
      <ToastContainer toasts={toasts} remove={removeToast} />

      {/* Upload progress overlay */}
      <AnimatePresence>
        {uploadState && (
          <UploadProgress
            filename={uploadState.filename}
            pct={uploadState.pct}
            status={uploadState.status}
          />
        )}
      </AnimatePresence>

      <Sidebar
        activeView={activeView}
        setActiveView={setActiveView}
        sessions={sessions}
        currentSessionId={currentSessionId}
        isAdmin={isAdmin}
        userRole={userRole}
        onNewChat={handleNewChat}
        onLoadSession={handleLoadSession}
        onDeleteSession={handleDeleteSession}
        onLogout={handleLogout}
      />

      {/* Main Content */}
      <div className="flex-1 flex flex-col relative bg-background/50 overflow-hidden">
        <ErrorBoundary>
          <Suspense fallback={<div className="flex-1 flex items-center justify-center text-muted-foreground animate-pulse">Loading module...</div>}>
            {activeView === 'settings' && (
              <SettingsPanel settings={settings} setSettings={setSettings} />
            )}

            {activeView === 'analytics' && <AnalyticsDashboard />}

            {activeView === 'documents' && (
              <DocumentManager onToast={addToast} onSelectDocument={setSelectedPdf} />
            )}

            {activeView === 'chat' && (
              <ChatView
                messages={messages}
                setMessages={setMessages}
                sessions={sessions}
                setSessions={setSessions}
                currentSessionId={currentSessionId}
                setCurrentSessionId={setCurrentSessionId}
                isStreaming={isStreaming}
                setIsStreaming={setIsStreaming}
                settings={settings}
                onToast={addToast}
                onUploadProgress={setUploadState}
                onSelectDocument={setSelectedPdf}
              />
            )}
          </Suspense>
        </ErrorBoundary>

        {/* Global PDF Drawer */}
        {selectedPdf && (
          <PdfDrawer
            filename={selectedPdf.filename}
            page={selectedPdf.page}
            snippet={selectedPdf.snippet}
            onClose={() => setSelectedPdf(null)}
          />
        )}
      </div>
    </div>
  );
}
