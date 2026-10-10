const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const duration = value =>
  Number.isFinite(value)
    ? value < 60
      ? value.toFixed(1) + ' s'
      : (value / 60).toFixed(1) + ' min'
    : 'Unavailable';
function union(intervals) {
  let total = 0,
    end = -Infinity;
  for (const [a, b] of intervals.sort((a, b) => a[0] - b[0])) {
    total += Math.max(0, b - Math.max(a, end));
    end = Math.max(end, b);
  }
  return total;
}
export function mountTiming(container, project, { reveal, native } = {}) {
  container.replaceChildren();
  container.classList.add('timing-view');
  let data = null,
    selected = null,
    follow = true,
    range = null,
    disposed = false,
    pending = false;
  const controls = el('div', undefined, 'timing-controls'),
    filter = el('select'),
    start = el('input'),
    end = el('input'),
    live = el('button', 'Return to live');
  filter.setAttribute('aria-label', 'Timing session scope');
  start.type = end.type = 'datetime-local';
  start.setAttribute('aria-label', 'Range start UTC');
  end.setAttribute('aria-label', 'Range end UTC');
  controls.append(filter, start, end, live);
  const nativeButton = el('button', 'Native experiment statistics');
  nativeButton.onclick = () => native?.();
  controls.append(nativeButton);
  for (const [label, factor, shift] of [
    ['Zoom in', 0.5, 0],
    ['Zoom out', 2, 0],
    ['Earlier', 1, -0.5],
    ['Later', 1, 0.5],
  ]) {
    const b = el('button', label);
    b.onclick = () => {
      if (!data) return;
      const [a, z] = range || data.range,
        w = Math.max(1, z - a),
        mid = (a + z) / 2 + shift * w;
      range = [mid - (w * factor) / 2, mid + (w * factor) / 2];
      follow = false;
      render();
    };
    controls.append(b);
  }
  const summary = el('div', undefined, 'timing-summary'),
    chart = el('div', undefined, 'timing-chart'),
    detail = el('article'),
    note = el('p');
  container.append(controls, summary, chart, detail, note);
  const iso = seconds => new Date(seconds * 1000).toISOString().slice(0, 19);
  const setRange = () => {
    const next = [Date.parse(start.value + 'Z') / 1000, Date.parse(end.value + 'Z') / 1000];
    if (!next.every(Number.isFinite) || next[1] <= next[0]) return;
    follow = false;
    range = next;
    render();
  };
  start.onchange = end.onchange = setRange;
  filter.onchange = () => {
    follow = false;
    const s = data.sessions.find(s => s.id === filter.value);
    range = s ? [s.start, s.end ?? (s.status === 'open' ? data.now : s.last)] : null;
    render();
  };
  live.onclick = () => {
    follow = true;
    range = null;
    filter.value = '';
    render();
  };
  function render() {
    if (!data) return;
    const focused = document.activeElement?.dataset.timingId;
    const [a, b] = range || data.range,
      span = Math.max(1, b - a),
      clip = row => [
        Math.max(a, row.start),
        Math.min(b, row.end ?? (row.status === 'open' ? data.now : row.last ?? data.now)),
      ];
    const sessions = data.sessions.map(clip).filter(([x, y]) => y > x);
    summary.replaceChildren(
      el('p', 'Wall-clock window · ' + duration(b - a)),
      el('p', 'Project-open union · ' + duration(union(sessions))),
      el(
        'p',
        'Summed captured tool time · ' +
          (data.tasks.some(t => t.end !== null)
            ? duration(
                data.tasks.reduce(
                  (sum, t) =>
                    sum +
                    (t.end === null ? 0 : Math.max(0, Math.min(b, t.end) - Math.max(a, t.start))),
                  0
                )
              )
            : 'Unavailable (no completed native tool timings)')
      )
    );
    start.value = iso(a);
    end.value = iso(b);
    live.disabled = follow;
    chart.replaceChildren();
    const heading = el('p', iso(a) + ' — ' + iso(b) + ' UTC');
    chart.append(heading);
    const rows = [
      ['Workflow stages (state occupancy)', data.stages],
      ['Project-open sessions', data.sessions],
      ['Native tool tasks', data.tasks],
    ];
    for (const [title, items] of rows) {
      chart.append(el('h3', title));
      for (const row of items) {
        const [x, y] = clip(row);
        if (y < a || x > b) continue;
        const track = el('div', undefined, 'timing-track'),
          name = row.label || row.id.slice(0, 8),
          label = el('span', name),
          bar = el('button', name, 'timing-span');
        bar.dataset.timingId = row.id;
        bar.style.left = ((x - a) / span) * 100 + '%';
        bar.style.width = Math.max(1, ((y - x) / span) * 100) + '%';
        bar.classList.toggle('selected', selected?.id === row.id);
        bar.title = `${name} · ${iso(row.start)} UTC · ${
          row.end === null ? 'end unknown or ongoing' : duration(row.end - row.start)
        } · ${row.status || 'workflow state'}`;
        bar.onclick = () => {
          selected = row;
          if (follow) range = [...data.range];
          follow = false;
          render();
        };
        const lane = el('div', undefined, 'timing-lane');
        lane.append(bar);
        track.append(label, lane);
        chart.append(track);
      }
    }
    detail.replaceChildren();
    if (selected) {
      const current =
        [...data.stages, ...data.sessions, ...data.tasks].find(row => row.id === selected.id) ||
        selected;
      const overlap = Math.max(
        0,
        Math.min(b, current.end ?? current.last ?? data.now) - Math.max(a, current.start)
      );
      detail.append(
        el('h3', current.label || 'Project session ' + current.id.slice(0, 8)),
        el(
          'p',
          'Full occurrence · ' +
            (current.end === null ? 'ongoing / end unknown' : duration(current.end - current.start))
        ),
        el('p', 'Overlap with window · ' + duration(overlap)),
        el('p', 'Source · ' + (current.source || 'recorded project lifecycle')),
        el(
          'p',
          current.counter_scope
            ? 'Count · ' + current.count + ' ' + current.unit + ' · ' + current.counter_scope
            : 'No per-occurrence counters recorded'
        )
      );
      if (current.receipt) {
        const button = el('button', 'Reveal checkpoint evidence');
        button.onclick = () => reveal?.(current.receipt);
        detail.append(button);
      }
    }
    note.textContent =
      data.clock +
      '. ' +
      data.task_measure +
      '. ' +
      data.open_measure +
      '. ' +
      data.warnings.join(' ');
    if (focused)
      chart
        .querySelector(`[data-timing-id="${CSS.escape(focused)}"]`)
        ?.focus({ preventScroll: true });
  }
  async function update() {
    if (disposed || pending) return;
    pending = true;
    try {
      const r = await fetch('/yapnr/api/timing/' + encodeURIComponent(project));
      if (!r.ok) throw Error('Timing evidence unavailable');
      data = await r.json();
      if (disposed) return;
      const prior = filter.value;
      filter.replaceChildren(el('option', 'All activity'));
      filter.firstChild.value = '';
      for (const s of data.sessions) {
        const o = el('option', s.id.slice(0, 8) + ' · ' + s.status);
        o.value = s.id;
        filter.append(o);
      }
      filter.value = prior;
      render();
    } catch (e) {
      note.textContent = e.message;
    } finally {
      pending = false;
    }
  }
  update();
  const timer = setInterval(update, 5000);
  return () => {
    disposed = true;
    clearInterval(timer);
  };
}
