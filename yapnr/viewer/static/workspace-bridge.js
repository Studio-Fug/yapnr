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
    if (data?.type === 'yapnr-pin-comparison') {
      api('/api/pin', {})
        .then(r =>
          post('yapnr-comparison-ready', { pin: r.pin_id, scope: scope(), viewport: { ...view } })
        )
        .catch(e => post('yapnr-view-error', { error: e.message }));
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
