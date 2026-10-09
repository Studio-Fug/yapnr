const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const kinds = {
  discovery: ['⌕', 'Discovery'],
  parts: ['◉', 'Part picking'],
  schematic: ['▤', 'Schematic build'],
  pnr: ['⌁', 'PCB placement & routing'],
  simulation: ['∿', 'Simulation'],
  validation: ['✓', 'Validation'],
  other: ['◇', 'Other'],
};
export function mountExperiments(container, { project, open, artifact, select, report } = {}) {
  container.replaceChildren();
  container.classList.add('experiment-browser');
  const toolbar = el('div', undefined, 'experiment-toolbar'),
    filter = el('input'),
    type = el('select'),
    count = el('span', '', 'muted'),
    tree = el('div', undefined, 'experiment-tree'),
    detail = el('section', undefined, 'experiment-detail');
  filter.placeholder = 'Find an experiment';
  filter.setAttribute('aria-label', 'Find an experiment');
  type.setAttribute('aria-label', 'Experiment type');
  const all = el('option', 'All types');
  all.value = '';
  type.append(all);
  for (const [key, [, label]] of Object.entries(kinds)) {
    const option = el('option', label);
    option.value = key;
    type.append(option);
  }
  toolbar.append(filter, type, count);
  tree.setAttribute('role', 'tree');
  tree.setAttribute('aria-label', 'Experiments');
  container.append(toolbar, tree, detail);
  let disposed = false,
    key = '',
    pending = false,
    runs = [],
    recorded = [],
    live = [],
    livePending = false,
    liveKey = '',
    selected = null;
  const file = ref => {
    const button = el('button', ref.path.split('/').pop(), 'experiment-file');
    button.title = ref.path + '\n' + (ref.sha256 || 'Hash unavailable');
    button.onclick = () => (ref.artifact && artifact ? artifact(ref.artifact) : open(ref.path));
    const line = el('div', undefined, 'experiment-reference');
    line.append(
      button,
      el('code', (ref.sha256 || '').slice(0, 12)),
      el('span', ref.verification, 'muted')
    );
    return line;
  };
  function choose(id) {
    selected = id;
    render();
    const run = runs.find(run => run.id === id);
    if (run?.lane) select?.(run);
  }
  function render() {
    const query = filter.value.toLowerCase(),
      matches = runs.filter(
        run =>
          (!type.value || run.kind === type.value) &&
          (run.id + ' ' + run.title).toLowerCase().includes(query)
      );
    count.textContent = matches.length + ' attempts';
    tree.replaceChildren();
    const included = new Set(matches.map(run => run.id));
    const add = (run, depth = 0, seen = new Set()) => {
      if (seen.has(run.id)) return;
      seen = new Set(seen).add(run.id);
      const [icon, label] = kinds[run.kind] || kinds.other;
      const button = el('button', undefined, 'experiment-attempt');
      button.dataset.attempt = run.id;
      button.title = run.id + (run.started ? '\n' + run.started : '');
      button.style.setProperty('--depth', depth);
      button.setAttribute('role', 'treeitem');
      button.setAttribute('aria-level', depth + 1);
      button.setAttribute('aria-selected', String(selected === run.id));
      const glyph = el('span', icon, 'experiment-kind');
      glyph.title = label;
      glyph.setAttribute('aria-label', label);
      button.append(
        glyph,
        el('span', run.title || run.id),
        el('span', run.status, 'experiment-status')
      );
      button.onclick = () => choose(run.id);
      tree.append(button);
      for (const child of matches.filter(child => child.parent === run.id))
        add(child, depth + 1, seen);
    };
    for (const run of matches.filter(run => !included.has(run.parent))) add(run);
    if (!matches.length)
      tree.append(
        el('p', runs.length ? 'No matching experiments.' : 'No experiments recorded yet.')
      );
    detail.replaceChildren();
    const run = runs.find(run => run.id === selected);
    if (!run) {
      detail.append(el('p', 'Select an attempt to browse its artifacts and provenance.', 'muted'));
      return;
    }
    const [, label] = kinds[run.kind] || kinds.other;
    detail.append(el('h3', run.title || run.id), el('p', label + ' · ' + run.status));
    if (run.title !== run.id) detail.append(el('code', run.id));
    if (run.seconds != null) detail.append(el('p', run.seconds + ' seconds', 'muted'));
    if (run.lane) {
      const view = el('button', 'Show in workspace');
      view.onclick = () => select?.(run);
      detail.append(view);
    }
    if (run.path) {
      const result = el('button', 'Open result');
      result.onclick = () => open(run.path);
      detail.append(result);
    }
    detail.append(el('h4', 'Inputs'));
    if (!run.inputs?.length)
      detail.append(el('p', run.provenance || 'Inputs not recorded', 'muted'));
    for (const ref of run.inputs || []) detail.append(file(ref));
    for (const link of run.dependencies || []) {
      const button = el(
        'button',
        '← ' + (runs.find(item => item.id === link.attempt)?.title || link.attempt),
        'experiment-link'
      );
      button.title = link.input + ' ← ' + link.output + '\n' + link.basis;
      button.onclick = () => choose(link.attempt);
      detail.append(button, el('small', link.basis, 'muted'));
    }
    detail.append(el('h4', 'Produced artifacts'));
    if (!run.outputs?.length) detail.append(el('p', 'No outputs recorded.', 'muted'));
    for (const ref of run.outputs || []) detail.append(file(ref));
    const downstream = runs.filter(item =>
      item.dependencies?.some(link => link.attempt === run.id)
    );
    if (downstream.length) {
      detail.append(el('h4', 'Used by'));
      for (const next of downstream) {
        const button = el('button', '→ ' + next.title, 'experiment-link');
        button.onclick = () => choose(next.id);
        detail.append(button);
      }
    }
    if (run.parameters && Object.keys(run.parameters).length) {
      const settings = el('details'),
        summary = el('summary', 'Parameters & seeds');
      settings.append(summary, el('pre', JSON.stringify(run.parameters, null, 2)));
      detail.append(settings);
    }
  }
  filter.oninput = render;
  type.onchange = render;
  tree.onkeydown = event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const buttons = [...tree.querySelectorAll('[role="treeitem"]')];
    const index = buttons.indexOf(document.activeElement);
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : Math.max(0, Math.min(buttons.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
    buttons[next]?.focus();
    event.preventDefault();
  };
  async function update() {
    if (disposed || pending) return;
    pending = true;
    try {
      const response = await fetch('/yapnr/api/experiments/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Experiments unavailable');
      const data = await response.json();
      if (disposed || JSON.stringify(data) === key) return;
      key = JSON.stringify(data);
      recorded = data.runs;
      merge();
    } catch (error) {
      report?.(error.message);
    } finally {
      pending = false;
    }
  }
  function merge() {
    const attached = new Set(recorded.map(run => run.lane).filter(Boolean));
    runs = [...recorded, ...live.filter(run => !attached.has(run.lane))];
    render();
  }
  async function updateLive() {
    if (disposed || livePending) return;
    livePending = true;
    try {
      const response = await fetch('/yapnr/api/experiment-lanes/' + encodeURIComponent(project));
      if (!response.ok) return;
      const data = await response.json();
      const next = JSON.stringify(data);
      if (disposed || next === liveKey) return;
      liveKey = next;
      live = data.runs;
      merge();
    } catch {
    } finally {
      livePending = false;
    }
  }
  update();
  updateLive();
  const timer = setInterval(() => {
    update();
    updateLive();
  }, 3000);
  return {
    reveal: choose,
    dispose() {
      disposed = true;
      clearInterval(timer);
    },
  };
}
