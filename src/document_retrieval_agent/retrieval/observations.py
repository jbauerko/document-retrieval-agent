"""DOM observation script used by download helpers and model-directed recovery.

Keep page-side JavaScript here so the browser adapter has no separate asset to load.
"""

OBSERVATION_SCRIPT = r"""
// Describe visible page content and assign short-lived references to controls.
// This uses DOM semantics, not identifiers belonging to the regulatory website.
generation => {
  const normalize = text => (text || '').replace(/\s+/g, ' ').trim();
  const visible = element => {
    const bounds = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return bounds.width > 0 && bounds.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  const controls = 'button, a[href], input, textarea, select, [role="button"], ' +
    '[role="textbox"], [contenteditable="true"], [tabindex]:not([tabindex="-1"])';
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible);
  const scope = dialogs.at(-1) || document.body;

  document.querySelectorAll('[data-agent-ref]').forEach(element =>
    element.removeAttribute('data-agent-ref'));

  const elements = [...scope.querySelectorAll(controls)].filter(element =>
    visible(element) && !element.querySelector(controls)).slice(0, 160).map((element, index) => {
    const ref = `e${generation}_${index + 1}`;
    element.setAttribute('data-agent-ref', ref);
    const bounds = element.getBoundingClientRect();
    const row = element.closest('tr, [role="row"]');
    const context = row || element.closest('label') || element.parentElement;
    const label = element.getAttribute('aria-label') || element.getAttribute('placeholder') ||
      normalize(element.innerText) || element.getAttribute('title') ||
      normalize(element.parentElement?.innerText);
    return {
      ref, tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role') || element.tagName.toLowerCase(),
      label: label.slice(0, 250),
      value: element.value ?? null,
      editable: element.isContentEditable || ['INPUT', 'TEXTAREA'].includes(element.tagName),
      disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
      href: element.getAttribute('href'),
      context: normalize(context?.innerText).slice(0, 500),
      position: {x: Math.round(bounds.x), y: Math.round(bounds.y)},
    };
  });

  const rows = [...scope.querySelectorAll('tr, [role="row"]')]
    .filter(element => visible(element) && element.querySelector('button, [role="button"], a[href]'))
    .slice(0, 25)
    .map(element => ({
      text: normalize(element.innerText).slice(0, 1000),
      refs: [...element.querySelectorAll('[data-agent-ref]')]
        .map(control => control.getAttribute('data-agent-ref')),
    }));

  return {
    text: (document.body.innerText || '').trim().slice(0, 14000),
    dialog: dialogs.length ? normalize(scope.innerText).slice(0, 1500) : null,
    elements, rows,
  };
}
"""
