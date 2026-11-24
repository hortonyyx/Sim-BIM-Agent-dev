import {element as el} from './dom.js';

function numbers(text) {
  const tokens = text.trim().split(/[\s,，;；]+/).filter(Boolean);
  if (tokens.some(token => !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(token))) return null;
  const values = tokens.map(Number);
  return values.every(value => Number.isFinite(value) && value > 0) ? values : null;
}

export function thicknessPicker(container, t, change) {
  const groups = new Map();
  let workspace;
  function read() {
    const result = {};
    for (const [key, group] of groups) {
      const custom = numbers(group.custom.value);
      group.custom.setCustomValidity(custom ? '' : t('invalidThickness'));
      group.custom.setAttribute('aria-invalid', String(!custom));
      if (!custom) return null;
      const values = [...group.chips.querySelectorAll('input:checked')].map(input => Number(input.value));
      const selected = [...new Set([...values, ...custom])].sort((a,b) => a-b);
      if (selected.length) result[key] = selected;
    }
    return result;
  }
  function sync(presets, selected, activeWorkspace, disabled) {
    for (const [key, values] of Object.entries(presets || {})) {
      let group = groups.get(key);
      if (!group) {
        const field = el('fieldset'), legend = el('legend'), chips = el('div','thickness-values');
        const label = el('label','thickness-custom'), labelText = el('span'), custom = el('input');
        field.dataset.group = key; custom.type = 'text'; custom.autocomplete = 'off';
        for (const value of values) {
          const chip = el('label'), input = el('input');
          input.type = 'checkbox'; input.value = String(value);
          chip.append(input, el('span','',value)); chips.append(chip);
        }
        const update = () => change(read());
        chips.addEventListener('change', update); custom.addEventListener('input', update);
        label.append(labelText,custom); field.append(legend,chips,label); container.append(field);
        group = {field,legend,chips,labelText,custom}; groups.set(key,group);
      }
      const chosen = selected?.[key] || [];
      group.field.disabled = disabled;
      group.legend.textContent = t(key+'Thickness'); group.labelText.textContent = t('customThickness');
      group.custom.placeholder = t('customThicknessHint');
      for (const input of group.chips.querySelectorAll('input')) input.checked = chosen.includes(Number(input.value));
      if (activeWorkspace !== workspace || (!group.custom.matches(':focus') && group.custom.validity.valid)) {
        group.custom.value = chosen.filter(n => !values.includes(n)).join(', ');
        group.custom.setCustomValidity(''); group.custom.removeAttribute('aria-invalid');
      }
    }
    workspace = activeWorkspace;
  }
  return {sync, valid: () => [...groups.values()].every(group => group.custom.validity.valid)};
}
