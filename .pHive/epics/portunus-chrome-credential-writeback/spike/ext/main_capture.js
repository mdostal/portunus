// MAIN world, document_start: try to capture the native store/ctor before any page script runs.
(function () {
  window.__spikeNative = { store: CredentialsContainer.prototype.store, creds: navigator.credentials, PC: window.PasswordCredential };
})();
