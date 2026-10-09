const el = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};
export function mountSearch(dialog, { project, nativeQuery, select, report } = {}) {
  const input = dialog.querySelector('input'),
    results = dialog.querySelector('[data-results]'),
    status = dialog.querySelector('[role="status"]');
  let generation = 0,
    timer,
    controller,
    local = [],
    native = [],
    savedProject = '',
    focus = 0;
  function draw() {
    const seen = new Set(),
      items = [...native, ...local]
        .filter(item => {
          const key = [item.kind, item.ref || item.name || item.file, item.line || ''].join(':');
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .slice(0, 100);
    results.replaceChildren();
    for (const item of items) {
      const button = el('button'),
        title = el('strong', item.ref || item.name || `${item.file}:${item.line}`);
      button.type = 'button';
      button.append(title, el('span', item.kind), el('small', item.text || item.detail || ''));
      button.onclick = () => {
        dialog.close();
        select(item);
      };
      results.append(button);
    }
    status.textContent = input.value.trim()
      ? `${items.length} ${items.length === 1 ? 'match' : 'matches'}${
          items.length === 100 ? ' · first 100 shown' : ''
        }`
      : 'Find components, nets, file names or source text.';
    focus = 0;
  }
  async function query(preserve = false) {
    const id = ++generation,
      name = project(),
      q = input.value.trim();
    controller?.abort();
    controller = new AbortController();
    if (!preserve) {
      local = [];
      native = [];
      draw();
    }
    if (!name || !q) return;
    status.textContent = 'Searching…';
    nativeQuery(q, id);
    try {
      const response = await fetch(
        `/yapnr/api/search/${encodeURIComponent(name)}?q=${encodeURIComponent(q)}`,
        { signal: controller.signal }
      );
      if (!response.ok) throw Error('Source search unavailable');
      const data = await response.json();
      if (id !== generation || name !== project()) return;
      local = data.results;
      draw();
    } catch (error) {
      if (error.name !== 'AbortError') report?.(error.message);
    }
  }
  input.oninput = () => {
    generation++;
    controller?.abort();
    clearTimeout(timer);
    timer = setTimeout(query, 120);
  };
  input.onkeydown = event => {
    const buttons = [...results.querySelectorAll('button')];
    if (event.key === 'ArrowDown' && buttons.length) {
      event.preventDefault();
      buttons[focus]?.focus();
    }
    if (event.key === 'Enter' && buttons.length) {
      event.preventDefault();
      buttons[0].click();
    }
  };
  results.onkeydown = event => {
    const buttons = [...results.querySelectorAll('button')],
      index = buttons.indexOf(document.activeElement);
    if (['ArrowUp', 'ArrowDown'].includes(event.key)) {
      event.preventDefault();
      focus = Math.max(
        0,
        Math.min(buttons.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1))
      );
      buttons[focus]?.focus();
    }
  };
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      dialog.close();
    }
  });
  return {
    shortcut() {
      clearTimeout(timer);
      if (savedProject !== project()) {
        savedProject = project();
        input.value = '';
        local = [];
        native = [];
        draw();
      }
      if (dialog.open) {
        input.value = '';
        query();
      } else {
        dialog.show();
        draw();
        if (input.value) query(true);
      }
      input.focus();
    },
    receive(id, items) {
      if (id !== generation) return;
      native = items;
      draw();
    },
    reset() {
      generation++;
      controller?.abort();
      clearTimeout(timer);
      input.value = '';
      local = [];
      native = [];
      dialog.close();
      draw();
    },
  };
}
