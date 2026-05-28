import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, BarChart2, Network } from 'lucide-react';
import {
  BarChart as RechartsBarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, AreaChart, Area,
} from 'recharts';
import type { AnalyticsData } from '../api';
import { getAnalytics, triggerGraphRAGSummaries } from '../api';

export function AnalyticsDashboard() {
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchAnalytics = useCallback(async () => {
    setLoading(true);
    try {
      setData(await getAnalytics());
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAnalytics(); }, [fetchAnalytics]);

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <RefreshCw className="w-8 h-8 text-primary animate-spin" />
      </div>
    );
  }
  if (!data) {
    return <div className="p-8 text-center text-muted-foreground">Failed to load analytics.</div>;
  }

  const totalEmployees = data.department_metrics.reduce((a, d) => a + d.employee_count, 0);
  const cacheHitRate = data.total_cache_entries > 0
    ? Math.round((data.cache_entries_last_24h / data.total_cache_entries) * 100)
    : 0;

  const handleGraphRAG = async () => {
    try {
      const res = await triggerGraphRAGSummaries();
      alert(`GraphRAG triggered: ${res.message}`);
    } catch (e: any) {
      alert(`Failed to trigger GraphRAG: ${e.message}`);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-10 space-y-8 bg-background">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold flex items-center gap-2">
          <BarChart2 className="w-6 h-6 text-primary" /> HR Analytics
        </h2>
        
        <div className="flex flex-col items-end gap-2 text-right">
            <div className="flex items-center gap-3">
            <button
                onClick={handleGraphRAG}
                className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm font-bold transition-all shadow-lg"
            >
                <Network className="w-4 h-4" />
                Build GraphRAG Summaries
            </button>
            <button
                onClick={fetchAnalytics}
                className="p-2 hover:bg-accent rounded-lg transition-colors text-muted-foreground hover:text-foreground"
            >
                <RefreshCw className="w-4 h-4" />
            </button>
            </div>
            <span className="text-[10px] text-muted-foreground max-w-xs leading-tight">
                * Nightly background job: extracts enterprise clusters from Neo4j into OpenSearch.
            </span>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: 'Departments',    value: data.department_metrics.length,   color: 'text-blue-400' },
          { label: 'Total Employees',value: totalEmployees,                    color: 'text-green-400' },
          { label: 'Cache Entries',  value: data.total_cache_entries,          color: 'text-purple-400' },
          { label: 'Cache 24h %',    value: `${cacheHitRate}%`,               color: 'text-amber-400' },
          { label: 'Sessions',       value: data.total_sessions,               color: 'text-pink-400' },
          { label: 'Unique Users',   value: data.unique_users,                 color: 'text-cyan-400' },
          { label: 'Cache (24h)',    value: data.cache_entries_last_24h,       color: 'text-indigo-400' },
          { label: 'Query Days',     value: data.query_volume_7d.length,       color: 'text-teal-400' },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-card border border-border rounded-xl p-4 shadow-sm">
            <p className="text-xs font-medium text-muted-foreground mb-1">{label}</p>
            <p className={`text-2xl font-bold ${color}`}>{value}</p>
          </div>
        ))}
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-card border border-border rounded-xl p-6 shadow-sm">
          <h3 className="text-sm font-semibold mb-4 text-muted-foreground uppercase tracking-wider">
            Avg Salary by Dept
          </h3>
          <ResponsiveContainer width="100%" height={220}>
            <RechartsBarChart data={data.department_metrics}>
              <CartesianGrid strokeDasharray="3 3" stroke="#333" />
              <XAxis dataKey="department" stroke="#888" tick={{ fontSize: 11 }} />
              <YAxis stroke="#888" tick={{ fontSize: 11 }} />
              <Tooltip contentStyle={{ backgroundColor: '#1e1e1e', borderColor: '#333', fontSize: 12 }} />
              <Bar dataKey="average_salary" fill="#3b82f6" name="Avg Salary ($)" radius={[4, 4, 0, 0]} />
            </RechartsBarChart>
          </ResponsiveContainer>
        </div>

        <div className="bg-card border border-border rounded-xl p-6 shadow-sm">
          <h3 className="text-sm font-semibold mb-4 text-muted-foreground uppercase tracking-wider">
            Query Volume (7 Days)
          </h3>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={data.query_volume_7d}>
              <defs>
                <linearGradient id="qvGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#333" />
              <XAxis dataKey="date" stroke="#888" tick={{ fontSize: 10 }} />
              <YAxis stroke="#888" tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip contentStyle={{ backgroundColor: '#1e1e1e', borderColor: '#333', fontSize: 12 }} />
              <Area type="monotone" dataKey="queries" stroke="#8b5cf6" fill="url(#qvGrad)" strokeWidth={2} name="Queries" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Recent Queries */}
      {data.recent_queries.length > 0 && (
        <div className="bg-card border border-border rounded-xl p-6 shadow-sm">
          <h3 className="text-sm font-semibold mb-4 text-muted-foreground uppercase tracking-wider">
            Recent Cached Queries
          </h3>
          <div className="space-y-2">
            {data.recent_queries.map((q, i) => (
              <div key={i} className="flex items-start gap-3 text-sm p-2 rounded-lg hover:bg-accent/50 transition-colors">
                <span className="text-muted-foreground font-mono text-xs mt-0.5 shrink-0">
                  {q.at.substring(0, 16)}
                </span>
                <span className="text-foreground/80 truncate">{q.query}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
