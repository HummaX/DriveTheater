import { MonitorDown } from 'lucide-react';
import type { Video } from './types';

export function DeviceDownload({ video }: { video: Video }) {
  if (!video.download_url) return null;
  return <a className="button button-small device-download" href={video.download_url} download aria-label={`Download original ${video.title} to your device`} aria-description="Download the complete original file from this server. Resume and byte ranges are supported; no video conversion or new provider download starts."><MonitorDown size={16} /><span>Download to device</span></a>;
}
