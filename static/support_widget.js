(() => {
  const form = document.getElementById('alport-cookie-form');
  if (!form) return;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const error = document.getElementById('alport-cookie-error');
    if (button.disabled) return;
    button.disabled = true;
    error.textContent = '';
    try {
      // A field named "action" shadows form.action. Read the HTML attribute.
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        headers: {'X-Requested-With': 'AlportSupport'}
      });
      if (!response.ok || !(await response.json()).ok) throw new Error('Save failed');
      document.getElementById('alport-cookie-notice').remove();
    } catch {
      error.textContent = 'Unable to save. Please try again, or open Cookie details to save your settings.';
      button.disabled = false;
    }
  });
})();
