import { useTranslation } from 'react-i18next';
import {
  MessageSquare, Shield, Settings as SettingsIcon, Languages, ChevronRight,
  Trash2, BarChart2, FolderOpen, LogOut,
} from 'lucide-react';
import type { ChatSession } from '../api';
import { cn } from '../lib/cn';

type ActiveView = 'chat' | 'settings' | 'analytics' | 'documents';

interface Props {
  activeView: ActiveView;
  setActiveView: (v: ActiveView) => void;
  sessions: ChatSession[];
  currentSessionId: string | null;
  isAdmin: boolean;
  userRole: string;
  onNewChat: () => void;
  onLoadSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onLogout: () => void;
}

export function Sidebar({
  activeView,
  setActiveView,
  sessions,
  currentSessionId,
  isAdmin,
  userRole,
  onNewChat,
  onLoadSession,
  onDeleteSession,
  onLogout,
}: Props) {
  const { t, i18n } = useTranslation();

  const navItems: { key: ActiveView; label: string; icon: React.ElementType; show: boolean }[] = [
    { key: 'settings',  label: t('settings.title') || 'Settings', icon: SettingsIcon, show: true },
    { key: 'analytics', label: t('nav.operationsAlerts', 'Operations Alerts'), icon: BarChart2, show: isAdmin },
    { key: 'documents', label: t('nav.documentLibrary', 'Document Library'), icon: FolderOpen, show: isAdmin },
  ];

  return (
    <div className="w-80 border-r border-border bg-card flex flex-col">
      {/* Logo */}
      <div className="p-6 border-b border-border flex items-center gap-3">
        <Shield className="w-8 h-8 text-primary" />
        <span className="font-bold text-xl truncate">KMG Kashagan AI</span>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-6">
        {/* Navigation */}
        <div className="space-y-2">
          {navItems.filter(n => n.show).map(nav => (
            <button
              key={nav.key}
              onClick={() => setActiveView(nav.key)}
              className={cn(
                'w-full flex items-center justify-between p-3 rounded-xl border transition-colors',
                activeView === nav.key
                  ? 'bg-primary/10 border-primary text-primary'
                  : 'bg-accent/50 hover:bg-accent border-border/50',
              )}
            >
              <div className="flex items-center gap-3">
                <nav.icon className="w-5 h-5" />
                <span className="font-bold text-sm">{nav.label}</span>
              </div>
              <ChevronRight className="w-4 h-4 opacity-60" />
            </button>
          ))}

          {/* Language Selector */}
          <div className="flex items-center gap-3 bg-accent/50 p-2 rounded-lg border border-border/50">
            <Languages className="w-4 h-4 text-primary" />
            <select
              value={i18n.language}
              onChange={e => i18n.changeLanguage(e.target.value)}
              className="bg-transparent text-sm w-full outline-none text-foreground font-medium cursor-pointer"
            >
              <option value="en" className="bg-[#0b0f19] text-[#f1f5f9]" style={{ backgroundColor: '#0b0f19', color: '#f1f5f9' }}>English</option>
              <option value="ru" className="bg-[#0b0f19] text-[#f1f5f9]" style={{ backgroundColor: '#0b0f19', color: '#f1f5f9' }}>Русский</option>
              <option value="kk" className="bg-[#0b0f19] text-[#f1f5f9]" style={{ backgroundColor: '#0b0f19', color: '#f1f5f9' }}>Қазақша</option>
            </select>
          </div>
        </div>

        {/* Chat Sessions */}
        <div className="pt-2 border-t border-border/50 space-y-2">
          <button
            onClick={onNewChat}
            className="w-full flex items-center gap-3 p-3 bg-primary text-primary-foreground font-bold rounded-xl hover:opacity-90 transition-all justify-center shadow-lg shadow-primary/20"
          >
            <MessageSquare className="w-5 h-5" /> {t('nav.newChat', 'New Chat')}
          </button>

          <div className="text-xs font-bold text-muted-foreground uppercase tracking-wider py-2">
            {t('nav.previousChats', 'Previous Chats')}
          </div>

          {sessions.map(s => (
            <button
              key={s.session_id}
              onClick={() => onLoadSession(s.session_id)}
              className={cn(
                'w-full text-left p-3 rounded-xl border transition-colors flex justify-between items-center group',
                currentSessionId === s.session_id
                  ? 'bg-accent border-primary'
                  : 'bg-transparent border-transparent hover:bg-accent/50',
              )}
            >
              <span className="truncate text-sm font-medium">{s.title}</span>
              <Trash2
                onClick={e => {
                  e.stopPropagation();
                  onDeleteSession(s.session_id);
                }}
                className="w-4 h-4 opacity-0 group-hover:opacity-100 min-w-4 text-muted-foreground hover:text-destructive transition-all"
              />
            </button>
          ))}
        </div>
      </div>

      {/* User Footer */}
      <div className="p-4 border-t border-border">
        <div className="flex items-center gap-3 p-3 bg-accent rounded-xl">
          <div className="w-10 h-10 bg-primary/20 rounded-full flex items-center justify-center text-primary font-bold">
            {isAdmin ? 'A' : 'E'}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-bold truncate">
              {isAdmin ? t('settings.admin', 'Operations-Admin') : t('settings.employee', 'Employee')}
            </div>
            <div className="text-[10px] text-muted-foreground uppercase">{userRole}</div>
          </div>
          <button
            onClick={onLogout}
            className="text-muted-foreground hover:text-destructive transition-colors"
          >
            <LogOut className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  );
}
