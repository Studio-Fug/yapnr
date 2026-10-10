// SPDX-License-Identifier: AGPL-3.0-or-later
// Native review surface; its SVG, entity traces and verdicts come from
// Studio-Fug/rules_requirements at aabecadd6e8710a11e99ccb366e63d858d92f6c6.
export function mountTraceability(container, data, { openArtifact, ask, select } = {}) {
  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const button = (text, fn) => {
    const b = el('button', text);
    b.onclick = fn;
    return b;
  };
  container.replaceChildren();
  container.classList.add('trace-browser');
  const head = el('div', undefined, 'trace-heading');
  head.append(
    el('h2', 'Requirements & risk review'),
    el('p', 'YAML is the source of truth. Links do not establish verification or user acceptance.')
  );
  container.append(head);
  if (data.empty) {
    container.append(
      el(
        'p',
        'No YAML model yet. The agent must maintain user needs, requirements, risks, mitigations and methods under requirements/.'
      )
    );
    return;
  }
  head.append(el('small', data.models.join(' · ')));
  const issues = el('details', undefined, 'trace-issues');
  issues.append(el('summary', `${data.issues.length} model validation issues`));
  for (const i of data.issues)
    issues.append(el('p', `${i.severity}: ${i.entity || ''} ${i.message} (${i.path}:${i.line})`));
  if (data.issues.length) issues.open = true;
  container.append(issues);
  const toolbar = el('div', undefined, 'trace-toolbar'),
    search = el('input'),
    kind = el('select');
  search.placeholder = 'Search requirements, risks or IDs';
  search.setAttribute('aria-label', search.placeholder);
  for (const [value, label] of [
    ['', 'All entities'],
    ['user_need', 'User needs'],
    ['requirement', 'Requirements'],
    ['risk', 'Risks'],
    ['mitigation', 'Mitigations'],
    ['test_method', 'Methods'],
  ]) {
    const o = el('option', label);
    o.value = value;
    kind.append(o);
  }
  toolbar.append(search, kind);
  container.append(toolbar);
  const split = el('div', undefined, 'trace-split'),
    list = el('nav', undefined, 'trace-list'),
    detail = el('article', undefined, 'trace-detail');
  split.append(list, detail);
  container.append(split);
  function show(row) {
    detail.replaceChildren();
    detail.append(
      el('h3', `${row.id} · ${row.title}`),
      el('span', row.status || 'UNVERIFIED', 'trace-verdict'),
      el('p', `${row.location.path}:${row.location.line}`, 'muted')
    );
    select?.({ kind: 'requirement', ref: row.id });
    for (const [key, value] of Object.entries(row.data)) {
      if (
        ['id', 'title'].includes(key) ||
        value === undefined ||
        value === '' ||
        (Array.isArray(value) && !value.length)
      )
        continue;
      const field = el('section');
      field.append(
        el('h4', key.replaceAll('_', ' ')),
        el('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2))
      );
      detail.append(field);
    }
    const trace = el('section');
    trace.append(el('h4', 'Traceability'));
    for (const [direction, rows] of [
      ['From', row.incoming],
      ['To', row.outgoing],
    ])
      for (const r of rows || []) {
        const target = data.entities.find(e => e.id === r.id);
        trace.append(
          button(
            `${direction}: ${r.relation} · ${r.id} · ${r.title}${r.missing ? ' (missing)' : ''}`,
            () => target && show(target)
          )
        );
      }
    detail.append(trace);
    const evidence = el('section');
    evidence.append(el('h4', 'Verification evidence'));
    if (!row.members?.length) evidence.append(el('p', 'No verification cases for this entity.'));
    for (const item of row.members || []) {
      const card = el('div', undefined, 'trace-evidence');
      card.append(
        el('strong', `${item.target || ''} · ${item.name || item.case || ''}`),
        el(
          'p',
          `${item.status || item.state || 'Not run'} · ${item.level || ''}${
            item.stale ? ' · stale' : ''
          }`
        )
      );
      for (const artifact of item.artifacts || [])
        card.append(button(artifact.title, () => openArtifact?.(artifact)));
      evidence.append(card);
    }
    detail.append(evidence);
    for (const gap of row.gaps || [])
      detail.append(el('p', gap.message || JSON.stringify(gap), 'trace-gap'));
    if (ask)
      detail.append(
        button('Discuss this entity', () =>
          ask(
            `Review ${row.id}: ${row.title}. Incorporate my refinements into the YAML model and reconcile dependent evidence.`
          )
        )
      );
  }
  function renderList() {
    list.replaceChildren();
    const q = search.value.toLowerCase();
    for (const row of data.entities.filter(
      e =>
        (!kind.value || e.kind === kind.value) &&
        `${e.id} ${e.title} ${e.data.description || ''}`.toLowerCase().includes(q)
    )) {
      const b = button(`${row.id} · ${row.title}`, () => show(row));
      b.append(el('small', `${row.kind.replaceAll('_', ' ')} · ${row.status || 'UNVERIFIED'}`));
      list.append(b);
    }
  }
  search.oninput = kind.onchange = renderList;
  renderList();
  if (data.entities[0]) show(data.entities[0]);
  const graph = el('details', undefined, 'trace-graph');
  graph.append(el('summary', 'Trace graph · needs → requirements → mitigations → risks'));
  const viewport = el('div', undefined, 'trace-viewport');
  viewport.tabIndex = 0;
  viewport.setAttribute('aria-label', 'Traceability graph. Scroll to zoom, drag to pan.');
  const parsed = new DOMParser().parseFromString(data.graph.svg, 'image/svg+xml'),
    svg = parsed.documentElement;
  if (svg.nodeName === 'svg') {
    // Accept only the renderer's inert SVG vocabulary; model text never becomes HTML.
    for (const n of [...svg.querySelectorAll('*')]) {
      if (
        ![
          'g',
          'rect',
          'text',
          'tspan',
          'path',
          'line',
          'polyline',
          'polygon',
          'circle',
          'ellipse',
          'defs',
          'marker',
          'a',
          'title',
          'style',
        ].includes(n.localName)
      ) {
        n.remove();
        continue;
      }
      for (const a of [...n.attributes])
        if (
          a.name.startsWith('on') ||
          (['href', 'xlink:href'].includes(a.name) && !a.value.startsWith('#'))
        )
          n.removeAttribute(a.name);
    }
    const imported = document.importNode(svg, true);
    viewport.append(imported);
    let box = (imported.getAttribute('viewBox') || '0 0 100 100').split(/\s+/).map(Number);
    const initial = [...box];
    const apply = () => imported.setAttribute('viewBox', box.join(' '));
    imported.removeAttribute('width');
    imported.removeAttribute('height');
    // Pan/zoom interaction adapted from rules_requirements' graph display surface.
    viewport.onwheel = e => {
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.12 : 1 / 1.12;
      const width = Math.min(initial[2] * 6, Math.max(initial[2] / 12, box[2] * factor));
      const ratio = width / box[2];
      box = [
        box[0] + (box[2] * (1 - ratio)) / 2,
        box[1] + (box[3] * (1 - ratio)) / 2,
        width,
        box[3] * ratio,
      ];
      apply();
    };
    let drag = null,
      moved = false;
    viewport.onpointerdown = e => {
      drag = { x: e.clientX, y: e.clientY, box: [...box] };
      moved = false;
    };
    viewport.onpointermove = e => {
      if (!drag) return;
      const dx = e.clientX - drag.x,
        dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) {
        moved = true;
        viewport.setPointerCapture(e.pointerId);
        const r = viewport.getBoundingClientRect();
        box = [
          drag.box[0] - (dx / r.width) * drag.box[2],
          drag.box[1] - (dy / r.height) * drag.box[3],
          drag.box[2],
          drag.box[3],
        ];
        apply();
      }
    };
    viewport.onpointerup = viewport.onpointercancel = () => (drag = null);
    viewport.onclick = e => {
      e.preventDefault();
      if (moved) {
        moved = false;
        return;
      }
      const id =
        e.target.closest('[data-id]')?.dataset.id ||
        e.target.closest('a')?.getAttribute('href')?.replace('#requirement:', '');
      const row = data.entities.find(r => r.id === id);
      if (row) show(row);
    };
    graph.append(
      button('Fit graph', () => {
        box = [...initial];
        apply();
      }),
      viewport
    );
  }
  container.append(graph);
  return {
    select: id => {
      const row = data.entities.find(e => e.id === id);
      if (row) show(row);
    },
  };
}
