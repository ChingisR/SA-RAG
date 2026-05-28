import { useTranslation } from 'react-i18next';
import { Settings as SettingsIcon } from 'lucide-react';
import type { QuerySettings } from '../api';

interface Props {
  settings: QuerySettings;
  setSettings: React.Dispatch<React.SetStateAction<QuerySettings>>;
}

export function SettingsPanel({ settings, setSettings }: Props) {
  const { t } = useTranslation();

  const sliders: { key: keyof QuerySettings; label: string; min: number; max: number; step: number }[] = [
    { key: 'similarity_top_k', label: t('settings.top_k'),          min: 1,  max: 50, step: 1   },
    { key: 'rerank_top_n',     label: t('settings.top_n'),          min: 1,  max: 10, step: 1   },
    { key: 'temperature',      label: t('settings.temperature'),    min: 0,  max: 1,  step: 0.1 },
  ];

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-10">
      <h2 className="text-2xl font-bold mb-6 flex items-center gap-2">
        <SettingsIcon className="w-6 h-6 text-primary" />
        {t('settings.modal_title', 'Generation Settings')}
      </h2>

      <div className="max-w-lg space-y-8">
        {/* Sliders */}
        {sliders.map(({ key, label, min, max, step }) => (
          <div key={key} className="space-y-2">
            <div className="flex justify-between items-center text-xs font-bold text-muted-foreground uppercase">
              <label>{label}</label>
              <span className="font-mono text-primary">{settings[key] as number}</span>
            </div>
            <input
              type="range"
              min={min}
              max={max}
              step={step}
              value={settings[key] as number}
              onChange={e => {
                const val = step < 1 ? parseFloat(e.target.value) : parseInt(e.target.value);
                setSettings(s => ({ ...s, [key]: val }));
              }}
              className="w-full h-1.5 bg-accent rounded-lg appearance-none cursor-pointer accent-primary"
            />
          </div>
        ))}

        {/* Output Thinking Toggle */}
        <div className="flex items-center justify-between p-4 bg-accent/30 border border-border/50 rounded-xl">
          <div>
            <span className="text-sm font-bold block">
              {t('settings.output_thinking', 'Output Thinking Process')}
            </span>
            <span className="text-xs text-muted-foreground">
              {t('settings.output_thinking_desc', 'Allow AI to stream raw thought reasoning')}
            </span>
          </div>
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              className="sr-only peer"
              checked={settings.output_thinking}
              onChange={e => setSettings(s => ({ ...s, output_thinking: e.target.checked }))}
            />
            <div className="w-11 h-6 bg-muted peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary" />
          </label>
        </div>

        {/* Framework Selection */}
        <div className="space-y-2">
          <label className="text-xs font-bold text-muted-foreground uppercase block">
            {t('settings.framework', 'Agent Orchestration Engine')}
          </label>
          <select
            value={settings.framework}
            onChange={e => setSettings(s => ({ ...s, framework: e.target.value }))}
            className="w-full bg-accent/30 border border-border/50 text-sm rounded-xl p-3 outline-none"
          >
            <option value="langgraph">{t('settings.langgraph_opt', 'LangGraph Workflow (Multi-Agent)')}</option>
            <option value="llamaindex">{t('settings.llamaindex_opt', 'LlamaIndex ReAct (Autonomous)')}</option>
          </select>
        </div>

        {/* Buttons */}
        <div className="flex gap-4">
          <button
            onClick={() => setSettings(s => ({
              ...s,
              framework: 'langgraph',
              similarity_top_k: 20,
              rerank_top_n: 5,
              temperature: 0.1,
              output_thinking: false,
            }))}
            className="px-4 py-2 bg-accent border border-border rounded-xl text-sm font-bold hover:bg-accent/80 transition-colors"
          >
            {t('settings.reset', 'Reset defaults')}
          </button>
          
          <button
            onClick={async () => {
              try {
                const { saveUserSettings } = await import('../api');
                await saveUserSettings(settings.framework, settings.output_thinking);
                alert(t('settings.saved_success', "Account Settings Saved!"));
              } catch (e) {
                alert(t('settings.saved_error', "Failed to save settings"));
              }
            }}
            className="px-6 py-2 bg-primary text-primary-foreground rounded-xl text-sm font-bold hover:opacity-90 transition-all shadow-lg flex-1"
          >
            {t('settings.save', 'Save Account Settings')}
          </button>
        </div>
      </div>
    </div>
  );
}
