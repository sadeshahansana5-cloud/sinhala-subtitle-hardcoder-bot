export type ApiJob = {
  id?: string | null; kind?: string; stage?: string; status?: string; progress?: number;
  elapsed?: number; eta?: number; current_bytes?: number; total_bytes?: number;
  can_cancel?: boolean; can_set_name?: boolean; output_name?: string; output_size?: number;
};
export type MediaTrack = { index: number; codec: string; lang: string; title?: string; channels?: number; default?: boolean; extractable?: boolean };
export type MediaInfo = { width: number; height: number; duration: number; fps: number; codec: string; audio: MediaTrack[]; subs: MediaTrack[] };
export type BotFont = { name: string; family: string; owned: boolean; size: number };
export type BotHistory = { job_id: string; kind: string; status: string; started_at: number; elapsed: number; error?: string; output_name?: string; output_size?: number; can_retry?: boolean };
export type DashboardData = {
  ok: boolean;
  video: null | { name: string; size: number; expires_at: number; media: MediaInfo; preview_url?: string };
  subtitle: { name: string; size: number; modified?: number; ready: boolean };
  active_job: ApiJob | null;
  pending_delivery: null | { name: string; auto_send_at: number; size: number; can_set_name: boolean; sending: boolean; preview_url?: string };
  history: BotHistory[];
  stats: { total: number; completed: number; failed: number; cancelled: number; today: number };
  daily: { day: string; count: number }[];
  storage_bytes: number;
  fonts: BotFont[];
  style: Record<string, string | number | boolean | null>;
  watermark: string;
  limits: { video_bytes: number; srt_bytes: number; font_bytes: number };
  public_url: string;
};

export interface TelegramWebApp {
  initData: string;
  initDataUnsafe?: { user?: { first_name?: string; username?: string } };
  ready?: () => void;
  expand?: () => void;
  close?: () => void;
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
  BackButton?: { show: () => void; hide: () => void; onClick: (fn: () => void) => void; offClick: (fn: () => void) => void };
  HapticFeedback?: { notificationOccurred: (kind: 'error' | 'success' | 'warning') => void; impactOccurred: (kind: 'light' | 'medium' | 'heavy') => void };
}
declare global { interface Window { Telegram?: { WebApp?: TelegramWebApp } } }

export function telegramApp() { return typeof window === 'undefined' ? undefined : window.Telegram?.WebApp; }
export function initData() { return telegramApp()?.initData || ''; }
export function firstName() { return telegramApp()?.initDataUnsafe?.user?.first_name || ''; }

function headersWithAuth(extra?: HeadersInit) {
  const headers = new Headers(extra || {});
  const data = initData();
  if (data) headers.set('X-Telegram-Init-Data', data);
  return headers;
}

export async function api<T = Record<string, unknown>>(path: string, options: RequestInit = {}): Promise<T> {
  if (!initData()) throw new Error('Telegram session data is missing. Open the app from the matching bot chat, using /start → Open App or the chat Menu button; do not open the HTML in a regular browser.');
  const headers = headersWithAuth(options.headers);
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...options, headers, cache: 'no-store' });
  const text = await response.text();
  let body: any = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { message: text }; }
  if (!response.ok) throw new Error(String(body.message || body.reason || `Request failed (${response.status})`));
  return body as T;
}

export function post<T = Record<string, unknown>>(path: string, body: unknown) {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export function uploadWithProgress<T = Record<string, unknown>>(path: string, file: File, onProgress: (value: number) => void) {
  return new Promise<T>((resolve, reject) => {
    if (!initData()) { reject(new Error('Telegram session data is missing. Open this app from the matching bot chat before uploading.')); return; }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('X-Telegram-Init-Data', initData());
    xhr.responseType = 'text';
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.max(0, Math.min(100, Math.round(event.loaded * 100 / event.total)))); };
    xhr.onerror = () => reject(new Error('Network connection interrupted during upload.'));
    xhr.onabort = () => reject(new Error('Upload cancelled.'));
    xhr.onload = () => {
      let body: any = {};
      try { body = xhr.responseText ? JSON.parse(xhr.responseText) : {}; } catch { body = { message: xhr.responseText }; }
      if (xhr.status >= 200 && xhr.status < 300) { onProgress(100); resolve(body as T); }
      else reject(new Error(String(body.message || body.reason || `Upload failed (${xhr.status})`)));
    };
    const form = new FormData(); form.append('file', file, file.name); xhr.send(form);
  });
}

export async function stylePreviewBlob() {
  if (!initData()) throw new Error('Telegram session data is missing. Open this app from the matching bot chat to render a preview.');
  const response = await fetch('/dashboard/api/style/preview', { method: 'POST', headers: headersWithAuth(), cache: 'no-store' });
  if (!response.ok) { const text = await response.text(); throw new Error(text || `Preview failed (${response.status})`); }
  return URL.createObjectURL(await response.blob());
}
