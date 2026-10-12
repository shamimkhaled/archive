import { useEffect, useState } from "react";
import { registerSW } from "virtual:pwa-register";

/** Registers the /app/ service worker and offers a reload when a new build is deployed. */
export function UpdatePrompt() {
  const [update, setUpdate] = useState<null | (() => Promise<void>)>(null);

  useEffect(() => {
    if (!("serviceWorker" in navigator) || import.meta.env.DEV) return;
    const updateSW = registerSW({
      onNeedRefresh: () => setUpdate(() => () => updateSW(true)),
    });
  }, []);

  if (!update) return null;
  return (
    <div className="toast" role="status">
      A new version of the archive app is available.
      <button type="button" onClick={() => void update()}>
        Reload
      </button>
    </div>
  );
}
