import { useEffect, useState } from 'react';
import { ActionButton } from './ActionButton';
import { activatePwaUpdate, pwaInstallContextAvailable, registerDriveTheaterPwa, type PwaRegistrationHandlers } from './pwa';

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
};

function isStandalone(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(display-mode: standalone)').matches === true;
}

/**
 * Optional, small install/status card. Rendering it does not imply that every
 * browser can install the app: a browser supplies the install event when eligible.
 */
export function PwaInstall() {
  const [installEvent, setInstallEvent] = useState<BeforeInstallPromptEvent | null>(null);
  const [installed, setInstalled] = useState(isStandalone);
  const [registration, setRegistration] = useState<ServiceWorkerRegistration | null>(null);
  const [updateReady, setUpdateReady] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const canRegister = typeof window !== 'undefined' && pwaInstallContextAvailable(window.location);

  useEffect(() => {
    if (!canRegister) return;
    const handlers: PwaRegistrationHandlers = {
      onOfflineReady: () => setMessage('Public interface files are ready. An offline launch still depends on a browser-cached document.'),
      onUpdateReady: () => setUpdateReady(true),
    };
    void registerDriveTheaterPwa(handlers).then(setRegistration).catch(() => {
      setMessage('The app shell could not be prepared on this browser.');
    });
    const onBeforeInstall = (event: Event) => {
      event.preventDefault();
      setInstallEvent(event as BeforeInstallPromptEvent);
    };
    const onInstalled = () => {
      setInstallEvent(null);
      setInstalled(true);
      setMessage('DriveTheater was installed.');
    };
    window.addEventListener('beforeinstallprompt', onBeforeInstall);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onBeforeInstall);
      window.removeEventListener('appinstalled', onInstalled);
    };
  }, [canRegister]);

  const install = async () => {
    if (!installEvent) return;
    await installEvent.prompt();
    const choice = await installEvent.userChoice;
    setInstallEvent(null);
    setMessage(choice.outcome === 'accepted' ? 'Installation was accepted.' : 'Installation was dismissed. You can use the browser menu later.');
  };

  const applyUpdate = () => {
    if (!registration) return;
    const reload = () => window.location.reload();
    navigator.serviceWorker.addEventListener('controllerchange', reload, { once: true });
    activatePwaUpdate(registration);
  };

  return <section className="card pwa-install" aria-labelledby="pwa-install-title">
    <div className="section-heading">
      <div>
        <h2 id="pwa-install-title">Install DriveTheater</h2>
        <p className="muted">The installed app stores only its public interface. Your library, account, streams, captions, and media are never saved by the app shell.</p>
      </div>
      {installEvent && <ActionButton className="button button-primary" onClick={() => void install()}>Install app</ActionButton>}
      {updateReady && <ActionButton className="button" onClick={applyUpdate}>Reload update</ActionButton>}
    </div>
    {!canRegister && <p className="muted">Installation and the offline app shell require HTTPS or localhost.</p>}
    {canRegister && !installEvent && !installed && <p className="muted">When your browser supports installation and this site is eligible, use its Install app or Add to Home Screen control.</p>}
    {installed && <p className="muted">This app is installed. New public-shell updates appear here when the browser has downloaded one.</p>}
    <p className="muted">For offline video, explicitly use <strong>Download to device</strong>; playback is not saved for offline use by this app shell.</p>
    {message && <p role="status" className="muted">{message}</p>}
  </section>;
}
