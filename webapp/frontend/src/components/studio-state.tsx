import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { api, firstName, initData, post, stylePreviewBlob, telegramApp, uploadWithProgress, type DashboardData } from '@/lib/bot-api';

export type Cue = { start: number; end: number; text: string };
export type SubtitleStyle = {
  font: string; size: number; primary: string; outline_c: string; outline: number; shadow: number;
  alignment: number; margin_h: number; margin_v: number; bold: boolean; spacing: number;
};
export const defaultStyle: SubtitleStyle = {
  font: '', size: 48, primary: '#FFFFFF', outline_c: '#000000', outline: 3, shadow: 1.5,
  alignment: 2, margin_h: 60, margin_v: 50, bold: true, spacing: 0,
};

type UploadState = { fileName: string; progress: number; phase: 'uploading' | 'processing' } | null;
type StudioState = {
  data: DashboardData | null; connected: boolean; loading: boolean; error: string; firstName: string;
  refresh: () => Promise<void>; loadServerCues: () => Promise<void>; file: File | null; videoUrl: string; duration: number; setDuration: (value: number) => void;
  uploadMedia: (file: File) => Promise<void>; uploadSyncFile: (file: File) => Promise<{ stage: string }>; uploadFont: (file: File) => Promise<void>;
  uploadState: UploadState; cues: Cue[]; subtitleName: string; readSubtitles: (file: File) => Promise<void>;
  style: SubtitleStyle; setStyle: (value: SubtitleStyle) => void; savedStyle: SubtitleStyle; saveStyle: () => Promise<void>;
  renderStylePreview: () => Promise<string>; delayMs: number; setDelayMs: (value: number) => void;
  watermarkText: string; setWatermarkText: (value: string) => void; saveWatermark: () => Promise<void>;
  outputName: string; setOutputName: (value: string) => void; operation: string; setOperation: (value: string) => void;
  mode: string; setMode: (value: string) => void; notice: string; notify: (message: string) => void;
  startJob: (payload: Record<string, unknown>) => Promise<void>; cancelJob: () => Promise<void>;
  retryJob: (jobId: string) => Promise<void>; deleteLibrary: (scope: 'video' | 'subtitle' | 'all') => Promise<void>;
  setOutputDeliveryName: (name: string, action?: 'set' | 'skip') => Promise<void>;
  cancelDelivery: () => Promise<void>; removeFont: (name: string) => Promise<void>;
  getLocalVideo: (file: File) => void; clearLocal: () => void;
};
const StudioContext = createContext<StudioState | null>(null);

function parseSrt(text: string): Cue[] {
  const rx = /(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})[^\r\n]*\r?\n([\s\S]*?)(?=\r?\n\s*\r?\n|$)/g;
  const seconds = (stamp: string) => { const m = stamp.replace(',', '.').match(/(\d+):(\d{2}):(\d{2})\.(\d{3})/); return m ? (Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3])) * 1000 + Number(m[4]) : NaN; };
  return [...text.matchAll(rx)].map((m) => ({ start: seconds(m[1]!), end: seconds(m[2]!), text: m[3]!.replace(/<[^>]*>/g, '').trim() })).filter((cue) => Number.isFinite(cue.start) && Number.isFinite(cue.end) && cue.end > cue.start && cue.text);
}

export function StudioProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [videoUrl, setVideoUrl] = useState('');
  const [duration, setDuration] = useState(0);
  const [cues, setCues] = useState<Cue[]>([]);
  const [subtitleName, setSubtitleName] = useState('');
  const [style, setStyleState] = useState<SubtitleStyle>(defaultStyle);
  const [savedStyle, setSavedStyle] = useState<SubtitleStyle>(defaultStyle);
  const [styleDirty, setStyleDirty] = useState(false);
  const [delayMs, setDelayMs] = useState(0);
  const [watermarkText, setWatermarkText] = useState('');
  const [watermarkDirty, setWatermarkDirty] = useState(false);
  const [outputName, setOutputName] = useState('');
  const [operation, setOperation] = useState('hardcode');
  const [mode, setMode] = useState('balanced');
  const [uploadState, setUploadState] = useState<UploadState>(null);
  const noticeTimer = useRef<number | undefined>(undefined);
  const pollLock = useRef(false);
  const serverCueKey = useRef('');
  const serverCueLoadedKey = useRef('');
  const localSubtitleSelected = useRef(false);
  const [userName, setUserName] = useState('');

  const notify = useCallback((message: string) => {
    setNotice(message); if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(''), 4000);
  }, []);
  const refresh = useCallback(async () => {
    if (pollLock.current) return;
    pollLock.current = true;
    try {
      const next = await api<DashboardData>('/dashboard/api/state');
      setData(next); setConnected(true); setError(''); setUserName(firstName());
      const cueKey = next.subtitle?.ready ? `${next.subtitle.name}:${next.subtitle.size}:${next.subtitle.modified || 0}` : '';
      if (cueKey !== serverCueKey.current) {
        serverCueKey.current = cueKey;
        serverCueLoadedKey.current = '';
        if (!localSubtitleSelected.current) {
          setCues([]);
          setSubtitleName(cueKey ? (next.subtitle.name || '') : '');
        }
      }
      if (!styleDirty) { const nextStyle = { ...defaultStyle, ...(next.style as Partial<SubtitleStyle>) }; setStyleState(nextStyle); setSavedStyle(nextStyle); }
      if (!watermarkDirty) setWatermarkText(next.watermark || '');
    } catch (e) {
      setConnected(false); setError(e instanceof Error ? e.message : 'Unable to connect to the bot.');
    } finally { setLoading(false); pollLock.current = false; }
  }, [styleDirty, watermarkDirty]);

  const loadServerCues = useCallback(async () => {
    const key = serverCueKey.current;
    if (!key || localSubtitleSelected.current || serverCueLoadedKey.current === key) return;
    try {
      const cueResult = await api<{ cues?: Cue[] }>('/dashboard/api/subtitle/cues');
      if (serverCueKey.current !== key || localSubtitleSelected.current) return;
      setCues(Array.isArray(cueResult.cues) ? cueResult.cues : []);
      setSubtitleName(data?.subtitle?.name || '');
      serverCueLoadedKey.current = key;
    } catch (e) {
      notify(e instanceof Error ? `Subtitle preview could not load: ${e.message}` : 'Subtitle preview could not load.');
    }
  }, [data, notify]);

  useEffect(() => {
    const tg = telegramApp();
    try { tg?.ready?.(); tg?.expand?.(); tg?.setHeaderColor?.('#141718'); tg?.setBackgroundColor?.('#141718'); } catch { /* SDK is optional outside Telegram */ }
    if (!connected) void refresh();
    const timer = window.setInterval(() => { if (connected && document.visibilityState === 'visible') void refresh(); }, data?.active_job ? 1000 : 15000);
    const onVisible = () => { if (connected && document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); };
  }, [refresh, connected, Boolean(data?.active_job)]);
  useEffect(() => () => { if (videoUrl) URL.revokeObjectURL(videoUrl); if (noticeTimer.current) window.clearTimeout(noticeTimer.current); }, [videoUrl]);

  const getLocalVideo = useCallback((next: File) => {
    if (!next.type.startsWith('video/') && !/\.(mp4|mkv|mov|avi|ts|m4v|webm)$/i.test(next.name)) { notify('Choose a supported video file.'); return; }
    const url = URL.createObjectURL(next); setVideoUrl((old) => { if (old) URL.revokeObjectURL(old); return url; });
    setFile(next); setDuration(0);
  }, [notify]);
  const readSubtitles = useCallback(async (next: File) => {
    try {
      const text = await next.text(); const parsed = parseSrt(text);
      if (!parsed.length) { notify('No valid subtitle cues were found in this SRT file.'); return; }
      localSubtitleSelected.current = true;
      setCues(parsed); setSubtitleName(next.name); notify(`${parsed.length} subtitle cues loaded for preview.`);
    } catch { notify('This subtitle file could not be read.'); }
  }, [notify]);
  const uploadMedia = useCallback(async (next: File) => {
    const isSrt = next.name.toLowerCase().endsWith('.srt');
    if (isSrt) await readSubtitles(next); else getLocalVideo(next);
    setUploadState({ fileName: next.name, progress: 0, phase: 'uploading' });
    try {
      await uploadWithProgress('/dashboard/api/upload', next, (progress) => setUploadState({ fileName: next.name, progress, phase: progress >= 100 ? 'processing' : 'uploading' }));
      setUploadState(null); notify(isSrt ? 'SRT uploaded and validated on the server.' : 'Video uploaded and inspected by the server.');
      await refresh();
    } catch (e) { setUploadState(null); const msg = e instanceof Error ? e.message : 'Upload failed.'; notify(msg); throw e; }
  }, [getLocalVideo, readSubtitles, notify, refresh]);
  const uploadSyncFile = useCallback(async (next: File) => {
    if (!next.name.toLowerCase().endsWith('.srt')) throw new Error('Select an .srt subtitle file.');
    setUploadState({ fileName: next.name, progress: 0, phase: 'uploading' });
    try {
      const result = await uploadWithProgress<{ stage: string }>('/dashboard/api/sync/upload', next, (progress) => setUploadState({ fileName: next.name, progress, phase: progress >= 100 ? 'processing' : 'uploading' }));
      setUploadState(null); await refresh(); return result;
    } catch (e) { setUploadState(null); throw e; }
  }, [refresh]);
  const uploadFont = useCallback(async (next: File) => {
    setUploadState({ fileName: next.name, progress: 0, phase: 'uploading' });
    try {
      await uploadWithProgress('/dashboard/api/font', next, (progress) => setUploadState({ fileName: next.name, progress, phase: progress >= 100 ? 'processing' : 'uploading' }));
      setUploadState(null); notify('Font uploaded, validated for Sinhala, and installed.'); await refresh();
    } catch (e) { setUploadState(null); throw e; }
  }, [notify, refresh]);
  const setStyle = useCallback((next: SubtitleStyle) => { setStyleState(next); setStyleDirty(true); }, []);
  const saveStyle = useCallback(async () => {
    const result = await post<{ style: Record<string, unknown> }>('/dashboard/api/style', { style });
    const next = { ...defaultStyle, ...(result.style as Partial<SubtitleStyle>) };
    setStyleState(next); setSavedStyle(next); setStyleDirty(false); notify('Subtitle style saved to your bot account.'); await refresh();
  }, [style, notify, refresh]);
  const renderStylePreview = useCallback(() => stylePreviewBlob(), []);
  const saveWatermark = useCallback(async () => {
    const result = await post<{ watermark: string }>('/dashboard/api/watermark', { text: watermarkText });
    setWatermarkText(result.watermark); setWatermarkDirty(false); notify('Watermark saved to your bot account.'); await refresh();
  }, [watermarkText, notify, refresh]);
  const startJob = useCallback(async (payload: Record<string, unknown>) => {
    await post('/dashboard/api/start', payload); notify('Processing job started. Progress is live in the Jobs screen.'); await refresh();
  }, [notify, refresh]);
  const cancelJob = useCallback(async () => { await post('/dashboard/api/cancel', {}); notify('Cancellation requested.'); await refresh(); }, [notify, refresh]);
  const retryJob = useCallback(async (jobId: string) => { await post('/dashboard/api/retry', { job_id: jobId }); notify('Retry started.'); await refresh(); }, [notify, refresh]);
  const deleteLibrary = useCallback(async (scope: 'video' | 'subtitle' | 'all') => { await post('/dashboard/api/delete', { scope }); notify('Selected server file(s) deleted.'); await refresh(); }, [notify, refresh]);
  const setOutputDeliveryName = useCallback(async (name: string, action: 'set' | 'skip' = 'set') => { await post('/dashboard/api/name', { action, name }); notify(action === 'skip' ? 'Original output filename retained.' : 'Output filename saved.'); await refresh(); }, [notify, refresh]);
  const cancelDelivery = useCallback(async () => { await post('/dashboard/api/delivery/cancel', {}); notify('Pending output discarded.'); await refresh(); }, [notify, refresh]);
  const removeFont = useCallback(async (name: string) => { await post('/dashboard/api/font/delete', { name }); notify('Uploaded font deleted.'); await refresh(); }, [notify, refresh]);
  const clearLocal = useCallback(() => { localSubtitleSelected.current = false; serverCueLoadedKey.current = ''; setFile(null); setVideoUrl((old) => { if (old) URL.revokeObjectURL(old); return ''; }); setDuration(0); setCues([]); setSubtitleName(''); }, []);
  const value = useMemo<StudioState>(() => ({
    data, connected, loading, error, firstName: userName, refresh, loadServerCues, file, videoUrl, duration, setDuration,
    uploadMedia, uploadSyncFile, uploadFont, uploadState, cues, subtitleName, readSubtitles,
    style, setStyle, savedStyle, saveStyle, renderStylePreview, delayMs, setDelayMs,
    watermarkText, setWatermarkText: (v) => { setWatermarkText(v); setWatermarkDirty(true); }, saveWatermark,
    outputName, setOutputName, operation, setOperation, mode, setMode, notice, notify,
    startJob, cancelJob, retryJob, deleteLibrary, setOutputDeliveryName, cancelDelivery, removeFont,
    getLocalVideo, clearLocal,
  }), [data, connected, loading, error, userName, refresh, loadServerCues, file, videoUrl, duration, uploadMedia, uploadSyncFile, uploadFont, uploadState, cues, subtitleName, readSubtitles, style, setStyle, savedStyle, saveStyle, renderStylePreview, delayMs, watermarkText, saveWatermark, outputName, operation, mode, notice, notify, startJob, cancelJob, retryJob, deleteLibrary, setOutputDeliveryName, cancelDelivery, removeFont, getLocalVideo, clearLocal]);
  return <StudioContext.Provider value={value}>{children}</StudioContext.Provider>;
}

export function useStudio() { const state = useContext(StudioContext); if (!state) throw new Error('StudioProvider is required'); return state; }
export { initData };
