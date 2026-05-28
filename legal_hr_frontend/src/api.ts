import axios from 'axios';

// The path where the React app is served (via Nginx or local build)
const API_BASE_URL = '/api';

// ── Typed Interfaces ────────────────────────────────────────────────────────
export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  id: string;
  sources?: Citation[];
}

export interface Citation {
  filename: string;
  page: string;
  snippet: string;
}

export interface ChatSession {
  session_id: string;
  title: string;
  created_at: string;
}

export interface QuerySettings {
  similarity_top_k: number;
  rerank_top_n: number;
  temperature: number;
  user_role: string;
  framework: string;
  output_thinking: boolean;
}

export interface IngestedDocument {
  filename: string;
  chunks: number;
  document_type: string;
  summary: string;
}

export interface AnalyticsData {
  department_metrics: { department: string; average_salary: number; employee_count: number }[];
  total_cache_entries: number;
  cache_entries_last_24h: number;
  query_volume_7d: { date: string; queries: number }[];
  recent_queries: { query: string; at: string }[];
  total_sessions: number;
  unique_users: number;
}

// ── Auth Interceptor ────────────────────────────────────────────────────────
// Automatically attaches the JWT bearer token to every outbound axios request.
// DO NOT manually add Authorization headers in individual API calls below —
// the interceptor handles this universally.
axios.interceptors.request.use(config => {
  const token = localStorage.getItem('access_token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// ── Error Interceptor ───────────────────────────────────────────────────────
// Handles 401 (Unauthorized) and general 5xx (Backend) errors.
axios.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401) {
      localStorage.removeItem('access_token');
      localStorage.removeItem('user');
      window.location.href = '/login';
    } else if (error.response?.status >= 500) {
      // Dispatch a custom event so the UI can toast the user
      window.dispatchEvent(new CustomEvent('api_error', { detail: error.response?.data?.detail || 'Server Error' }));
    }
    return Promise.reject(error);
  }
);

// ── Auth ────────────────────────────────────────────────────────────────────
export const login = async (email: string, password: string) => {
  const response = await axios.post(`${API_BASE_URL}/login`, { email, password });
  return response.data; // { access_token, user }
};

// ── File Upload (with optional progress callback) ──────────────────────────
export const uploadPdf = async (
  file: File,
  onProgress?: (pct: number) => void
) => {
  const formData = new FormData();
  formData.append('file', file);
  const response = await axios.post(`${API_BASE_URL}/upload-pdf`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: e => {
      if (onProgress && e.total) onProgress(Math.round((e.loaded * 100) / e.total));
    },
  });
  return response.data;
};

// ── Task Status ─────────────────────────────────────────────────────────────
export const getTaskStatus = async (taskId: string) => {
  const response = await axios.get(`${API_BASE_URL}/task-status/${taskId}`);
  return response.data as { task_id: string; status: string; detail: string };
};

// ── Audio ───────────────────────────────────────────────────────────────────
export const transcribeAudio = async (blob: Blob) => {
  const formData = new FormData();
  formData.append('file', blob, 'audio.webm');
  const response = await axios.post(`${API_BASE_URL}/transcribe`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data as { status: string; text: string };
};

// ── Sessions ────────────────────────────────────────────────────────────────
export const getSessions = async (): Promise<ChatSession[]> => {
  const response = await axios.get(`${API_BASE_URL}/sessions`);
  return response.data;
};

export const createSession = async (title: string = 'New Chat'): Promise<ChatSession> => {
  const response = await axios.post(`${API_BASE_URL}/sessions?title=${encodeURIComponent(title)}`, {});
  return response.data;
};

export const getSessionMessages = async (sessionId: string): Promise<ChatMessage[]> => {
  const response = await axios.get(`${API_BASE_URL}/sessions/${sessionId}/messages`);
  return response.data;
};

export const deleteSession = async (sessionId: string) => {
  const response = await axios.delete(`${API_BASE_URL}/sessions/${sessionId}`);
  return response.data;
};

// ── Documents ───────────────────────────────────────────────────────────────
export const listDocuments = async (): Promise<{ documents: IngestedDocument[]; total: number }> => {
  const response = await axios.get(`${API_BASE_URL}/documents`);
  return response.data;
};

export const deleteDocument = async (filename: string) => {
  const response = await axios.delete(`${API_BASE_URL}/documents/${encodeURIComponent(filename)}`);
  return response.data;
};

// ── Feedback ────────────────────────────────────────────────────────────────
export const submitFeedback = async (messageId: string, rating: 1 | -1, comment?: string) => {
  const response = await axios.post(`${API_BASE_URL}/feedback`, { message_id: messageId, rating, comment });
  return response.data;
};

// ── Analytics ───────────────────────────────────────────────────────────────
export const getAnalytics = async (): Promise<AnalyticsData> => {
  const response = await axios.get(`${API_BASE_URL}/analytics`);
  return response.data;
};

export const triggerGraphRAGSummaries = async () => {
  const response = await axios.post(`${API_BASE_URL}/graphrag/build-summaries`);
  return response.data;
};

// ── User Settings ──────────────────────────────────────────────────────────
export const getUserSettings = async (): Promise<{ preferred_agent: string; output_thinking: boolean }> => {
  const response = await axios.get(`${API_BASE_URL}/settings`);
  return response.data;
};

export const saveUserSettings = async (preferred_agent: string, output_thinking: boolean) => {
  const response = await axios.post(`${API_BASE_URL}/settings`, { preferred_agent, output_thinking });
  return response.data;
};

// ── Query Streaming ──────────────────────────────────────────────────────────
export const streamQuery = async (
  query: string,
  chatHistory: ChatMessage[],
  settings: QuerySettings,
  onChunk: (chunk: string) => void,
  onCitations: (citations: Citation[]) => void,
  sessionId?: string
) => {
  const token = localStorage.getItem('access_token');
  const response = await fetch(`${API_BASE_URL}/query`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      query,
      chat_history: chatHistory.map(({ role, content }) => ({ role, content })),
      session_id: sessionId,
      ...settings,
    }),
  });

  if (!response.body) throw new Error('No body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let citationBuffer = '';
  let isCitationMode = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });

    if (chunk.includes('<!--CITATIONS_JSON:')) {
      const parts = chunk.split('<!--CITATIONS_JSON:');
      if (parts[0]) onChunk(parts[0]);
      isCitationMode = true;
      citationBuffer = parts[1];
    } else if (isCitationMode) {
      citationBuffer += chunk;
    } else {
      onChunk(chunk);
    }
  }

  if (isCitationMode) {
    const jsonStr = citationBuffer.split('-->')[0];
    try {
      onCitations(JSON.parse(jsonStr));
    } catch (e) {
      console.error('Failed to parse citations', e);
    }
  }
};
