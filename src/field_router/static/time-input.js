// Locale-independent time entry. Values stay HH:MM for the existing APIs.
(function () {
  'use strict';
  const isTime = input => input instanceof HTMLInputElement && input.hasAttribute('data-time24');
  document.addEventListener('input', event => {
    const input = event.target;
    if (!isTime(input) || event.isComposing) return;
    // Numeric phone keyboards need no colon key: typing 1830 produces 18:30.
    // Keep deletion and edits in the middle predictable.
    if (!event.inputType?.startsWith('delete') &&
        input.selectionStart === input.value.length && /^[0-9]{3,4}$/.test(input.value)) {
      input.value = input.value.slice(0, 2) + ':' + input.value.slice(2);
    }
  }, true);
  document.addEventListener('blur', event => {
    const input = event.target;
    if (!isTime(input)) return;
    const value = input.value.trim();
    const match = /^([0-9]{1,2}):([0-9]{2})$/.exec(value);
    if (match) input.value = match[1].padStart(2, '0') + ':' + match[2];
    else if (/^[0-9]{4}$/.test(value)) input.value = value.slice(0, 2) + ':' + value.slice(2);
    // HTML pattern and existing payload validation reject out-of-range values.
  }, true);
})();
