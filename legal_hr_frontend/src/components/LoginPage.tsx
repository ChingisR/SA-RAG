import { Shield, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

interface Props {
  email: string;
  setEmail: (v: string) => void;
  password: string;
  setPassword: (v: string) => void;
  error: string;
  onSubmit: (e: React.FormEvent) => void;
}

export function LoginPage({ email, setEmail, password, setPassword, error, onSubmit }: Props) {
  const { t, i18n } = useTranslation();

  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4 relative">
      <div className="absolute top-4 right-4">
        <select
          value={i18n.language}
          onChange={e => i18n.changeLanguage(e.target.value)}
          className="bg-accent border border-border rounded-lg px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary text-foreground cursor-pointer"
        >
          <option value="en">English</option>
          <option value="ru">Русский</option>
          <option value="kk">Қазақша</option>
        </select>
      </div>
      <div className="w-full max-w-md bg-card border border-border p-8 rounded-2xl shadow-2xl">
        <div className="flex flex-col items-center mb-8">
          <div className="p-4 bg-primary/10 rounded-2xl mb-4">
            <Shield className="w-12 h-12 text-primary" />
          </div>
          <h1 className="text-3xl font-bold text-foreground text-center">{t('welcome.title')}</h1>
          <p className="text-muted-foreground text-center mt-2">{t('welcome.subtitle')}</p>
        </div>
        <form onSubmit={onSubmit} className="space-y-6">
          {error && (
            <div className="p-3 bg-destructive/10 border border-destructive/20 text-destructive text-sm rounded-lg">
              {error}
            </div>
          )}
          <div className="space-y-2">
            <label className="text-sm font-medium text-muted-foreground">{t('auth.username')}</label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              className="w-full bg-accent border-none rounded-xl p-3 focus:ring-2 focus:ring-primary transition-all"
              placeholder="admin@enterprise.com"
              required
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-muted-foreground">{t('auth.password')}</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              className="w-full bg-accent border-none rounded-xl p-3 focus:ring-2 focus:ring-primary transition-all"
              placeholder="••••••••"
              required
            />
          </div>
          <button
            type="submit"
            className="w-full bg-primary text-primary-foreground font-bold p-4 rounded-xl hover:opacity-90 transition-all flex items-center justify-center gap-2"
          >
            {t('auth.submit')} <ChevronRight className="w-5 h-5" />
          </button>
        </form>
      </div>
    </div>
  );
}
