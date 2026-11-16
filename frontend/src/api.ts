import type { Job, Settings, Source, Video } from './types';

export interface DiscoverMovie { id: string; title: string; poster: string | null; year: string; rating: string; description: string; genres: string[] }

export interface TorrentioStream {
  name: string;
  title: string;
  magnet_url: string;
}

export interface TorrentioStreamResult {
  items: TorrentioStream[];
  unsupported_count: number;
}

export interface TorrentioEpisodeResult {
  id: string;
  title: string;
  poster: string | null;
  episodes: Array<{ id: string; title: string; season: number; episode: number; thumbnail: string | null; released: string | null }>;
}

export class ApiError extends Error { constructor(message: string, public status?: number) { super(message); } }

async function request<T>(path: string, options: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { credentials: 'same-origin', signal, ...options, headers });
  if (!response.ok) {
    let detail = 'The request could not be completed.';
    try { detail = (await response.json() as { detail?: string }).detail || detail; } catch { /* non-JSON response */ }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown, signal?: AbortSignal) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }, signal);

export const api = {
  me: (signal?: AbortSignal) => request<{ username: string }>('/api/auth/me', {}, signal),
  login: (username: string, password: string) => post<{ username: string }>('/api/auth/login', { username, password }),
  logout: () => post<void>('/api/auth/logout'),
  changePassword: (current_password: string, new_password: string) => post<{ ok: true }>('/api/auth/password', { current_password, new_password }),
  videos: (search: string, state: string, signal?: AbortSignal) => request<{ items: Video[]; total: number }>(`/api/videos?${new URLSearchParams({ search, state }).toString()}`, {}, signal),
  video: (id: string, signal?: AbortSignal) => request<Video>(`/api/videos/${id}`, {}, signal),
  play: (id: string) => post<{ video: Video; job: Job | null; playback: Video['playback'] }>(`/api/videos/${id}/play`),
  download: (id: string) => post<{ video: Video; job: Job | null; playback: Video['playback'] }>(`/api/videos/${id}/download`),
  deleteCache: (id: string) => request<{ ok: true }>(`/api/videos/${id}/cache`, { method: 'DELETE' }),
  activity: (id: string, fields: { active: boolean; position?: number; duration?: number }) => post<{ ok: true }>(`/api/videos/${id}/activity`, fields),
  clearActivity: () => post<{ ok: true }>('/api/activity/clear'),
  uploadSubtitle: (id: string, file: File) => { const form = new FormData(); form.append('file', file); return request<Video>(`/api/videos/${id}/subtitles`, { method: 'POST', body: form }); },
  jobs: (signal?: AbortSignal) => request<{ items: Job[] }>('/api/jobs', {}, signal),
  cancelJob: (id: string) => post<Job>(`/api/jobs/${id}/cancel`),
  retryJob: (id: string) => post<Job>(`/api/jobs/${id}/retry`),
  sources: (signal?: AbortSignal) => request<{ items: Source[] }>('/api/sources', {}, signal),
  addSource: (body: Record<string, unknown>) => post<Source>('/api/sources/google', body),
  testSource: (id: string) => post<{ ok: true; message: string }>(`/api/sources/${id}/test`),
  syncSource: (id: string) => post<{ source: Source; added: number; updated: number }>(`/api/sources/${id}/sync`),
  oauthStart: (id: string) => post<{ url: string }>(`/api/sources/${id}/oauth/start`),
  deleteSource: (id: string) => request<{ ok: true }>(`/api/sources/${id}`, { method: 'DELETE' }),
  addUrl: (body: { url: string; engine: 'auto'; title?: string; download?: boolean }) => post<Video & { reused?: boolean }>('/api/urls', body),
  torrentioStreams: (body: { manifest_url?: string; media_type: 'movie' | 'series'; media_id: string }) => post<TorrentioStreamResult>('/api/torrentio/streams', body),
  discoveryConfig: () => request<{ torrentio_configured: boolean; metadata_configured: boolean }>('/api/discover/config'),
  saveDiscoveryConfig: (body: { torrentio_manifest_url?: string; metadata_manifest_url?: string }) => post<{ torrentio_configured: boolean; metadata_configured: boolean }>('/api/discover/config', body),
  discoverMovies: (body: { query: string; skip: number }) => post<{ items: DiscoverMovie[]; skip: number }>('/api/discover/catalog', body),
  torrentioEpisodes: (body: { media_id: string; metadata_manifest_url?: string }) => post<TorrentioEpisodeResult>('/api/torrentio/episodes', body),
  settings: (signal?: AbortSignal) => request<Settings>('/api/settings', {}, signal),
  updateSettings: (body: Partial<Pick<Settings, 'cache_limit_gb' | 'min_free_gb' | 'download_concurrency' | 'max_transcodes' | 'download_schedule_enabled' | 'download_start_hour' | 'download_end_hour' | 'download_limit_mbps' | 'auto_cache_cleanup' | 'cache_cleanup_target_percent' | 'activity_retention_days'>>) => request<Settings>('/api/settings', { method: 'PATCH', body: JSON.stringify(body) }),
  cleanup: () => post<{ removed: number; freed_bytes: number }>('/api/cache/cleanup'),
  alerts: (signal?: AbortSignal) => request<AlertsSnapshot>('/api/alerts', {}, signal),
  server: (signal?: AbortSignal) => request<ServerSnapshot>('/api/server', {}, signal),
  activityLog: (limit = 100, signal?: AbortSignal) => request<ActivityLog>('/api/activity?' + new URLSearchParams({ limit: String(limit) }), {}, signal)
};

export interface ServerSnapshot {
  scope: 'container_namespace' | 'host_or_runtime_namespace';
  cpu: { percent: number; cores: number };
  memory: { total_bytes: number; used_bytes: number };
  disk: { path: string; total_bytes: number; used_bytes: number; free_bytes: number };
  network: { bytes_sent: number; bytes_recv: number; send_rate_bps: number | null; recv_rate_bps: number | null };
  uptime_seconds: number;
  process: { pid: number; uptime_seconds: number; rss_bytes: number };
  database: { sqlite_bytes: number; wal_bytes: number };
  counts: { users: number; sessions: number; sources: number; videos: number; jobs: number; activity_events: number; watch_history: number };
  active_jobs: number;
  downloaders: Record<string, { available: boolean; detail: string }>;
  media_cpu: { configured_cpus: number | null; effective_cpus: number | null; scope_note: string };
}

export interface AlertsSnapshot {
  items: Array<{ id: string; level: string; title: string; detail: string }>;
  checked_at: string;
}

export interface ActivityLog {
  items: Array<{ occurred_at: string; created_at: string; username: string; event_type: 'login_success' | 'login_failure' | 'logout' | 'play' | 'download' | 'device_download' | 'watch'; video_id: string | null; details: Record<string, unknown> | null }>;
  retention_days: number;
}
