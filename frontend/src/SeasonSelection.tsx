import { useState } from 'react';
import { ConfirmedSeasonDownload } from './LibraryTools';
import type { Video } from './types';

export function SeasonSelection({ episodes, onChanged }: { episodes: Video[]; onChanged?: () => void }) {
  const [selected, setSelected] = useState<string[]>([]);
  return <details className="season-selection"><summary>Select episodes to save to server</summary>
    <p className="muted small">Only selected local catalog entries are considered. Review the plan before confirming; no arbitrary stream is chosen.</p>
    <div className="episode-selection">{episodes.map(episode => <label className="checkbox" key={episode.id}><input type="checkbox" checked={selected.includes(episode.id)} onChange={event => setSelected(ids => event.target.checked ? [...ids, episode.id] : ids.filter(id => id !== episode.id))} />{episode.title}</label>)}</div>
    <ConfirmedSeasonDownload key={selected.join(',')} videoIds={selected} onChanged={onChanged} />
  </details>;
}
