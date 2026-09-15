(() => {
  const windowPanel = document.querySelector('[data-recording-window]');
  const form = document.querySelector('[data-recording-form]');
  if (!windowPanel || !form) return;
  const notice = windowPanel.querySelector('[data-window-notice]');
  const boundary = Date.parse(windowPanel.dataset.boundary);
  let dirty = false, saving = false;
  form.addEventListener('input', () => { dirty = true; });
  form.addEventListener('change', () => { dirty = true; });
  const check = () => {
    if (Date.now() < boundary) return;
    document.querySelectorAll('[data-early-context]').forEach(section => { section.hidden = true; });
    if (dirty || saving || form.getAttribute('aria-busy') === 'true') {
      notice.hidden = false;
    } else {
      window.location.reload();
    }
  };
  setInterval(check, 15000);
  window.addEventListener('pageshow', check);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  form.addEventListener('submit', async (event) => {
    // The shared attachment handler supplies progress and error preservation for uploads.
    if (event.defaultPrevented) return;
    event.preventDefault();
    if (saving) return;
    const body = new FormData(form);
    if (event.submitter?.name) body.append(event.submitter.name, event.submitter.value);
    const controls = [...form.elements].map(control => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    saving = true;
    form.setAttribute('aria-busy', 'true');
    try {
      // Controls named "action" mask the form.action DOM property.
      const destination = form.getAttribute('action') || window.location.href;
      const response = await fetch(destination, {method: 'POST', body, headers: {'Accept': 'application/json'}});
      const payload = await response.json();
      if (!response.ok || !payload.redirect) throw new Error(payload.error || 'The save could not be confirmed. Your input is still here.');
      window.location.assign(payload.redirect);
    } catch (error) {
      let feedback = form.querySelector('[data-recording-error]');
      if (!feedback) {
        feedback = document.createElement('p');
        feedback.dataset.recordingError = '';
        feedback.className = 'message error';
        feedback.setAttribute('role', 'alert');
        form.append(feedback);
      }
      feedback.textContent = error.message || 'The save could not be confirmed. Your input is still here.';
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
      // The shared upload handler disabled file inputs for Save Draft; restore them on error.
      form.querySelectorAll('input[type="file"]').forEach(input => { input.disabled = false; });
      saving = false;
      form.removeAttribute('aria-busy');
      dirty = true;
      check();
    }
  });
})();
