'use strict';
// Adapt this renderer instance to the one project shell. No sessions or backend actions are started here.
(function () {
  const mode = new URLSearchParams(location.search).get('embed');
  if (
    window.parent === window ||
    ![
      'board',
      'schematic',
      'three',
      'experiments',
      'performance',
      'controls',
      'exploration',
      'timing',
      'ask',
      'source',
    ].includes(mode)
  )
    return;
  let origin;
  try {
    origin = new URL(document.referrer).origin;
  } catch {
    return;
  }
  document.body.dataset.embed = mode;
  if (mode === 'performance') {
    const panel = document.createElement('section');
    panel.id = 'performance-panel';
    const controls = document.getElementById('performance-controls');
    if (controls) {
      controls.open = true;
      panel.append(controls);
    }
    document.querySelector('main').append(panel);
  }
  document.addEventListener(
    'keydown',
    event => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        event.stopPropagation();
        post('yapnr-search-shortcut', {});
      }
    },
    { capture: true }
  );
  function post(type, data) {
    window.parent.postMessage({ type, ...data }, origin);
  }
  function scope() {
    try {
      const l = lane();
      return {
        lane: laneId,
        phase,
        board_sha256: window.YapnrView?.boardSha?.() || null,
        layout_sha256: (phase === 'live' ? l : l?.frames?.[Number(phase)])?.layout_sha256 || null,
        source_revision: 'unknown',
      };
    } catch {
      return { source_revision: 'unknown' };
    }
  }
  if (['board', 'schematic', 'three'].includes(mode)) {
    schSetMode(mode === 'board' ? 'pcb' : mode === 'schematic' ? 'sch' : '3d', false);
    for (const event of ['pointerdown', 'wheel', 'keydown'])
      document.addEventListener(
        event,
        () => post('yapnr-central-interaction', { scope: scope() }),
        { capture: true, passive: true }
      );
  }
  if (['timing', 'ask', 'source'].includes(mode)) window.YapnrDock?.tab(mode);
  if (mode === 'experiments') {
    for (const event of ['click', 'keydown'])
      document.getElementById('lanes-panel')?.addEventListener(
        event,
        () => {
          const before = laneId;
          queueMicrotask(() => {
            if (laneId && laneId !== before)
              post('yapnr-experiment-selection', { lane: laneId, scope: scope() });
          });
        },
        { capture: true }
      );
  }
  const allowed = new Set([
    'changes',
    'labels',
    'air',
    'costs',
    'note-badges',
    'phase',
    'pause',
    'fit',
    'component-query',
    'component-clear',
  ]);
  function controls() {
    const items = [...document.querySelectorAll('#layers input')].map((e, i) => ({
      id: 'layer:' + i,
      label: e.parentElement.textContent.trim(),
      type: e.type,
      value: e.value,
      checked: e.checked,
    }));
    for (const id of allowed) {
      const e = document.getElementById(id);
      if (e)
        items.push({
          id,
          label: e.closest('label')?.textContent.trim() || e.textContent || e.title || id,
          type: e.tagName === 'BUTTON' ? 'button' : e.type || 'select',
          value: e.value,
          checked: e.checked,
          options:
            e.tagName === 'SELECT'
              ? [...e.options].map(o => ({ value: o.value, label: o.textContent }))
              : undefined,
        });
    }
    return items;
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== origin) return;
    const data = event.data;
    if (data?.type === 'yapnr-search' && typeof data.query === 'string') {
      const q = data.query.trim().toLowerCase().slice(0, 200),
        results = [],
        seen = new Set();
      const index = SRC()?.index?.(),
        geometry = geo();
      function add(selection, text) {
        const name = selection.ref || selection.name,
          key = selection.kind + ':' + name;
        if (!name || seen.has(key) || results.length >= 100) return;
        seen.add(key);
        results.push({
          kind: selection.kind,
          ref: selection.ref,
          name: selection.name,
          text,
          selection,
          scope: scope(),
        });
      }
      if (q) {
        for (const part of geometry?.parts || []) {
          const record = index?.components?.[part.ref] || {};
          const text = [part.ref, record.type, record.part, record.instance, record.value]
            .filter(Boolean)
            .join(' · ');
          if (text.toLowerCase().includes(q)) add({ kind: 'component', ref: part.ref }, text);
        }
        const names = new Set(Object.keys(index?.nets || {}));
        for (const part of geometry?.parts || [])
          for (const pad of part.pads || []) if (pad.net) names.add(pad.net);
        for (const track of geometry?.tracks || []) if (track[0]) names.add(track[0]);
        for (const name of [...names].sort())
          if (String(name).toLowerCase().includes(q))
            add({ kind: 'net', name }, SRC()?.netLabel?.(name) || name);
      }
      post('yapnr-search-results', { queryId: data.queryId, results });
      return;
    }
    if (data?.type === 'yapnr-pin-comparison') {
      api('/api/pin', {})
        .then(r =>
          post('yapnr-comparison-ready', { pin: r.pin_id, scope: scope(), viewport: { ...view } })
        )
        .catch(e => post('yapnr-view-error', { error: e.message }));
    }
    if (data?.type === 'yapnr-select-lane' && typeof data.lane === 'string') {
      window.YapnrView?.selectLane(data.lane);
      reportScope();
    }
    if (data?.type === 'yapnr-view-controls')
      post('yapnr-controls-state', { scope: scope(), items: controls() });
    if (data?.type === 'yapnr-view-command') {
      const id = data.id,
        e = id?.startsWith('layer:')
          ? document.querySelectorAll('#layers input')[+id.slice(6)]
          : allowed.has(id)
            ? document.getElementById(id)
            : null;
      if (!e) return;
      if (e.tagName === 'BUTTON') e.click();
      else {
        if (e.type === 'checkbox') e.checked = !!data.checked;
        else e.value = String(data.value ?? '');
        e.dispatchEvent(new Event('change', { bubbles: true }));
        e.dispatchEvent(new Event('input', { bubbles: true }));
      }
      post('yapnr-controls-state', { scope: scope(), items: controls() });
    }
    if (data?.type === 'yapnr-linked-selection') {
      const selected = data.selection;
      const from = data.scope?.board_sha256 || data.scope?.layout_sha256,
        to = scope().board_sha256 || scope().layout_sha256;
      if (!from || !to || from !== to) {
        post('yapnr-context-mismatch', { scope: scope() });
        return;
      }
      if (!selected) return;
      // Highlight is separate from navigation. Reveal is the only centering action.
      const target =
        selected.kind === 'component'
          ? { refs: [selected.ref] }
          : selected.kind === 'net'
            ? { nets: [selected.name] }
            : selected.kind === 'pad'
              ? { pads: [selected.ref + '.' + selected.pad] }
              : null;
      if (target) window.YapnrView?.highlight(target, { frame: !!data.reveal });
      if (data.reveal) SRC()?.inspect?.(selected, { focus: false });
    }
    if (data?.type === 'yapnr-theme') {
      document.documentElement.dataset.theme = data.theme === 'dark' ? 'dark' : 'light';
      document.documentElement.dataset.reduceTransparency = String(!!data.reduceTransparency);
      try {
        gestureCacheDirty = true;
        render();
      } catch {}
    }
  });
  const query = new URLSearchParams(location.search),
    checkpoint = query.get('checkpoint');
  if (checkpoint && /^[a-f0-9]{64}$/.test(checkpoint)) {
    (async () => {
      try {
        const snapshot = await api('/api/pins/' + checkpoint),
          id = query.get('lane');
        if (!snapshot.lanes[id]) throw Error('Pinned lane unavailable');
        pinned = snapshot;
        pinId = checkpoint;
        laneId = id;
        phase = query.get('phase') || 'live';
        phaseGeo =
          phase === 'live'
            ? null
            : await api(
                '/api/geometry/' +
                  (lane().frames[Number(phase)]?.board_sha256 ||
                    lane().frames[Number(phase)]?.layout_sha256)
              );
        updateControls();
        fit();
        render();
      } catch (e) {
        post('yapnr-view-error', { error: e.message });
      }
    })();
  }
  let lastScope = '';
  function reportScope() {
    const current = scope(),
      key = JSON.stringify(current);
    if (key !== lastScope) {
      lastScope = key;
      post('yapnr-view-ready', { scope: current, mode });
    }
  }
  reportScope();
  setInterval(reportScope, 1500);
})();
