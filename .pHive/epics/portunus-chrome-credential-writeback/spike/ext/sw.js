// Spike-only service worker. Harness calls self.spikeStore(...) via CDP. Never logs the value.
self.spikeStore = async (tabId, world, id, password, mode) => {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, world, args: [id, password, mode],
    func: async (id, password, mode) => {
      try {
        let store, PC, creds;
        if (mode === 'native-captured' && window.__spikeNative) { store = window.__spikeNative.store; PC = window.__spikeNative.PC; creds = window.__spikeNative.creds; }
        else { creds = navigator.credentials; store = creds.store; PC = window.PasswordCredential; }
        const hasActivation = navigator.userActivation ? navigator.userActivation.isActive : null;
        await store.call(creds, new PC({ id, password }));
        return { ok: true, hasActivation };
      } catch (e) { return { ok: false, err: e.name + ':' + String(e.message).slice(0, 120) }; }
    } });
  return r.result;
};
self.spikeTabs = async () => (await chrome.tabs.query({})).map(t => ({ id: t.id, url: t.url }));
