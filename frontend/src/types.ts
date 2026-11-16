export type VideoState = 'cloud' | 'queued' | 'downloading' | 'preparing' | 'playable' | 'cached' | 'cancelled' | 'failed';

export interface AudioTrack { index: number; label: string; language: string; default: boolean }
export interface SubtitleTrack { id: string; label: string; language: string; url: string }
export interface Playback { mode: 'direct' | 'hls' | null; url: string | null; available_until: number | null; duration: number | null }
export interface Video {
  id: string; title: string; source_id: string; source_kind: 'google_drive' | 'http' | 'yt-dlp' | 'mega' | 'torrent' | 'jdownloader'; remote_id: string;
  state: VideoState; mime_type: string | null; size_bytes: number | null; bytes_downloaded: number; duration: number | null;
  width: number | null; height: number | null; thumbnail_url: string | null; poster_url: string | null; preview_url: string | null;
  sprites_vtt_url: string | null; error: string | null; audio_tracks: AudioTrack[]; subtitle_tracks: SubtitleTrack[];
  playback: Playback; last_played: string | null; watch_position?: number | null;
  caption_status?: 'pending' | 'extracting' | 'generating' | 'ready' | 'unavailable' | 'failed'; caption_error?: string | null;
  updated_at: string; download_url?: string | null;
}
export interface Job {
  id: string; video_id: string; title: string; state: string; phase: string; progress: number;
  progress_known?: boolean; progress_unit?: 'bytes' | 'media_seconds' | 'items' | 'indeterminate';
  bytes_downloaded: number; total_bytes: number | null; error: string | null; attempt: number; updated_at: string;
}
export interface Source {
  id: string; name: string; kind: 'google_drive'; auth_mode: 'public_link' | 'api_key' | 'service_account' | 'oauth'; folder_id: string;
  status: string; video_count: number; last_sync: string | null; error: string | null; has_credentials: boolean; service_account_email: string | null; oauth_redirect_url: string | null;
  sync_progress?: { phase: string; processed: number; total: number | null } | null;
}
export interface Settings { cache_limit_gb: number; min_free_gb: number; download_concurrency: number; max_transcodes: number; cache_used_bytes: number; disk_free_bytes: number; data_dir: string; ffmpeg_available: boolean; worker_online: boolean; download_schedule_enabled?: boolean; download_start_hour?: number; download_end_hour?: number; download_limit_mbps?: number; auto_cache_cleanup?: boolean; cache_cleanup_target_percent?: number; activity_retention_days?: number }
export interface SpriteCue { start: number; end: number; image: string; x: number; y: number; width?: number; height?: number }
