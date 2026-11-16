import { useEffect, useRef, useState } from 'react';
import { ActionButton } from './ActionButton';
import { LibraryInspector } from './LibraryTools';
import { MetadataTools } from './MetadataTools';
import { StorageTools } from './StorageTools';
import { VideoDiagnostics } from './DiagnosticsDetails';
import { request } from './api';
import type { Video } from './types';

export function LibraryDetails({ video, onClose, onChanged }: { video: Video; onClose: () => void; onChanged: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [collections, setCollections] = useState<Array<{ id: string; name: string }>>([]);
  const [message, setMessage] = useState('');
  useEffect(() => { dialog.current?.showModal(); void request<{ items: Array<{ id: string; name: string }> }>('/api/library/collections').then(result => setCollections(result.items)).catch(() => setMessage('Collections could not be loaded.')); }, []);
  const membership = async (id: string, remove: boolean) => {
    try {
      const base = `/api/library/collections/${encodeURIComponent(id)}/items`;
      await request(remove ? `${base}/${encodeURIComponent(video.id)}` : base, { method: remove ? 'DELETE' : 'POST', body: remove ? undefined : JSON.stringify({ video_id: video.id }) });
      setMessage(remove ? 'Removed from collection.' : 'Added to collection.'); onChanged();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Collection update failed.'); }
  };
  return <dialog ref={dialog} className="library-details-dialog" onCancel={event => { event.preventDefault(); onClose(); }} onClose={onClose} aria-label={`Details for ${video.title}`}>
    <header className="section-heading"><h2>{video.title}</h2><ActionButton className="button" onClick={onClose}>Close</ActionButton></header>
    <LibraryInspector item={{ ...video, watched: video.watched === true }} onChanged={onChanged} />
    <section aria-label="Collection membership"><h3>Collections</h3>{collections.length ? collections.map(collection => <div className="action-row" key={collection.id}><span>{collection.name}</span><ActionButton className="button button-small" onClick={() => void membership(collection.id, false)}>Add</ActionButton><ActionButton className="button button-small" onClick={() => void membership(collection.id, true)}>Remove</ActionButton></div>) : <p>Create a collection in Library tools first.</p>}</section>
    <VideoDiagnostics videoId={video.id} />
    <MetadataTools videoId={video.id} initialType={video.type} onChanged={onChanged} />
    <details><summary>Storage and verification for this title</summary><StorageTools videoId={video.id} /></details>
    {message && <p role="status">{message}</p>}
  </dialog>;
}
