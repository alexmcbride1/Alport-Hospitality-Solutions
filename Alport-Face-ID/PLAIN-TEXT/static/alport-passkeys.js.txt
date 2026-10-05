/* Native WebAuthn ceremonies; verification and permissions remain on the server. */
(() => {
  'use strict';
  const root = document.querySelector('[data-passkey-lane]');
  if (!root) return;
  const lane = root.dataset.passkeyLane;
  const status = root.querySelector('[data-passkey-status]');
  const login = root.querySelector('[data-passkey-login]');
  const prepare = root.querySelector('[data-passkey-prepare]');
  const create = root.querySelector('[data-passkey-create]');
  const list = root.querySelector('[data-passkey-list]');
  let csrf, ready, preparedAt, busy = false;
  const tell = text => { status.textContent = text; };
  const decode = value => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(value.length / 4) * 4, '=')), c => c.charCodeAt(0));
  const encode = value => {
    if (value == null) return null;
    let binary = '';
    new Uint8Array(value).forEach(b => { binary += String.fromCharCode(b); });
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };
  function options(data) {
    const result = {...data, challenge: decode(data.challenge)};
    if (data.user) result.user = {...data.user, id: decode(data.user.id)};
    for (const field of ['allowCredentials', 'excludeCredentials']) {
      if (data[field]) result[field] = data[field].map(item => ({...item, id: decode(item.id)}));
    }
    return result;
  }
  function serialize(credential) {
    if (!credential) throw new Error('No passkey was returned. You can still sign in with your password.');
    const r = credential.response;
    const response = {clientDataJSON: encode(r.clientDataJSON)};
    for (const key of ['attestationObject', 'authenticatorData', 'signature', 'userHandle']) {
      if (key in r) response[key] = encode(r[key]);
    }
    if (r.getTransports) response.transports = r.getTransports();
    return {id: credential.id, rawId: encode(credential.rawId), type: credential.type,
      response, clientExtensionResults: credential.getClientExtensionResults(),
      authenticatorAttachment: credential.authenticatorAttachment};
  }
  async function api(path, data) {
    const response = await fetch('/api/passkeys/' + path, {
      method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Passkey-CSRF': csrf || ''},
      ...(data === undefined ? {} : {body: JSON.stringify(data)})
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || 'The request failed. Reload the page or use your password.');
    return body;
  }
  function explain(error) {
    if (error.name === 'NotAllowedError' || error.name === 'AbortError') {
      return 'The device prompt was cancelled or timed out. Try again, or use your password.';
    }
    if (error.name === 'InvalidStateError') return 'A passkey for this account is already saved on that device. Try another device or password manager.';
    if (error.name === 'SecurityError' || error.name === 'NotSupportedError') return 'Passkeys are unavailable here. Open Alport directly in a supported browser, or use your password.';
    return error.message || 'Passkey setup failed. Your password still works.';
  }
  async function prepareLogin() {
    ready = null;
    login.disabled = true;
    ready = await api(lane + '/authenticate/options', {});
    preparedAt = Date.now();
    login.disabled = false;
  }
  if (login) login.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    login.disabled = true;
    try {
      if (!ready || Date.now() - preparedAt > 240000) {
        await prepareLogin();
        tell('Ready. Press Face ID / passkey again to open your device prompt.');
        return;
      }
      const flow = ready.flow;
      // Call the browser API immediately inside the click, after prefetching options.
      const pending = navigator.credentials.get({publicKey: options(ready.options)});
      ready = null;
      tell('Follow the prompt on your device.');
      const credential = await pending;
      const result = await api(lane + '/authenticate/verify', {flow, credential: serialize(credential)});
      location.assign(result.redirect);
    } catch (error) {
      tell(explain(error));
      try { await prepareLogin(); } catch (retryError) { tell(explain(retryError)); }
    } finally { busy = false; }
  });
  function dateText(value) { return value ? new Date(value * 1000).toLocaleDateString() : 'Not used yet'; }
  async function refresh() {
    const result = await api(lane + '/list');
    list.replaceChildren();
    if (!result.keys.length) { list.textContent = 'No passkeys added yet.'; return; }
    for (const key of result.keys) {
      const item = document.createElement('article');
      item.className = 'passkey-record';
      const name = document.createElement('h3');
      name.textContent = key.label;
      const detail = document.createElement('p');
      detail.textContent = (key.valid ? 'Active' : 'Inactive — password changed') + ' · Added ' + dateText(key.created_at) + ' · Last used: ' + dateText(key.last_used);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = 'Remove ' + key.label;
      remove.addEventListener('click', async () => {
        const field = root.querySelector('[data-passkey-remove-password]');
        if (!field.value) { tell('Enter your current password in the removal field first.'); field.focus(); return; }
        if (!window.confirm('Remove “' + key.label + '” from this Alport account?')) return;
        remove.disabled = true;
        try {
          const password = field.value;
          field.value = '';
          await api(lane + '/revoke/' + key.id, {password});
          tell('Passkey removed from Alport. You can also delete its saved entry in your device’s password manager.');
          await refresh();
        } catch (error) { tell(explain(error)); remove.disabled = false; }
      });
      item.append(name, detail, remove);
      list.append(item);
    }
  }
  if (prepare) prepare.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    const submit = prepare.querySelector('button');
    submit.disabled = true;
    create.hidden = true;
    ready = null;
    const passwordField = prepare.elements.namedItem('password');
    const password = passwordField.value;
    passwordField.value = '';
    try {
      ready = await api(lane + '/register/options', {password, label: prepare.elements.namedItem('label').value});
      preparedAt = Date.now();
      create.hidden = false;
      create.disabled = false;
      tell('Password confirmed. Press “Save passkey on this device” to continue.');
      create.focus();
    } catch (error) { tell(explain(error)); }
    finally { busy = false; submit.disabled = false; }
  });
  if (create) create.addEventListener('click', async () => {
    if (busy) return;
    if (!ready || Date.now() - preparedAt > 240000) {
      tell('Setup expired. Enter your password again to prepare a new passkey.');
      create.hidden = true;
      return;
    }
    busy = true;
    create.disabled = true;
    try {
      const flow = ready.flow;
      const pending = navigator.credentials.create({publicKey: options(ready.options)});
      ready = null;
      tell('Follow the prompt on your device to save your passkey.');
      const credential = await pending;
      await api(lane + '/register/verify', {flow, credential: serialize(credential)});
      tell('Passkey saved. You can now choose Face ID / passkey on the sign-in page.');
      await refresh();
    } catch (error) { tell(explain(error)); }
    finally { busy = false; create.hidden = true; }
  });
  (async () => {
    try {
      const config = await api('config');
      csrf = config.csrf;
      if (list) await refresh();
      if (!config.enabled) {
        tell('To set up or use passkeys, open https://alporthospitality.co.uk. Password sign-in is still available.');
        return;
      }
      if (!window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials) {
        tell('This browser does not support passkeys. Password sign-in is still available.');
        return;
      }
      if (login) { await prepareLogin(); tell('Already enabled a passkey? Use your device to sign in.'); }
      if (prepare) prepare.querySelector('button').disabled = false;
    } catch (error) { tell(explain(error)); }
  })();
})();
