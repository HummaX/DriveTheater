import { MonitorDown } from 'lucide-react';
import type { Video } from './types';

export function DeviceDownload({ video }: { video: Video }) {
  if (!video.download_url) return null;
  const mime = video.mime_type || '';
  const ext = mime.includes('matroska') ? '.mkv' : (mime.includes('webm') ? '.webm' : '.mp4');
  const filename = video.title
    ? (video.title.toLowerCase().endsWith('.mp4') || video.title.toLowerCase().endsWith('.mkv') || video.title.toLowerCase().endsWith('.webm')
        ? video.title
        : `${video.title}${ext}`)
    : `video${ext}`;
  return <a className="button button-small device-download" href={video.download_url} download={filename} aria-label={`Download original ${video.title} to your device`} aria-description="Download the complete original file from this server. Resume and byte ranges are supported; no video conversion or new provider download starts."><MonitorDown size={16} /><span>Download to device</span></a>;
}
