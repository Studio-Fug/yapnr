const snapshots = new Map();
async function snapshot(project) {
  let cached = snapshots.get(project);
  if (cached && Date.now() - cached.time < 750) return cached.promise;
  const promise = fetch('/yapnr/api/design/' + encodeURIComponent(project)).then(async response => {
    if (!response.ok) throw Error('Source connection unavailable');
    return response.json();
  });
  snapshots.set(project, { time: Date.now(), promise });
  return promise;
}
const el = (tag, text) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  return n;
};
export function mountSource(container, { project, path, onOpen, onDirty, report } = {}) {
  container.replaceChildren();
  container.classList.add('source-view');
  const head = el('div'),
    label = el('p'),
    chooser = el('select'),
    edit = el('textarea'),
    actions = el('div');
  chooser.setAttribute('aria-label', 'Source file');
  edit.setAttribute('aria-label', 'Atopile source');
  edit.spellcheck = false;
  edit.readOnly = true;
  const start = el('button', 'Edit working tree'),
    save = el('button', 'Save source'),
    cancel = el('button', 'Discard edits');
  save.hidden = cancel.hidden = true;
  head.append(label, chooser);
  actions.append(start, save, cancel);
  container.append(head, edit, actions);
  let current = null,
    dirty = false,
    editing = false,
    selected = path || '',
    disposed = false,
    pending = false;
  const mark = value => {
    if (dirty === value) return;
    dirty = value;
    container.dataset.dirty = String(value);
    onDirty?.(value);
  };
  chooser.onchange = () => {
    if (onOpen) onOpen(chooser.value);
    else {
      selected = chooser.value;
      update();
    }
  };
  const draftKey = () => 'yapnr.source-draft.v1.' + project + ':' + selected;
  const stash = () => {
    try {
      if (dirty)
        localStorage.setItem(
          draftKey(),
          JSON.stringify({ sha256: current.sha256, baseline: current.text, text: edit.value })
        );
      else localStorage.removeItem(draftKey());
    } catch {
      report?.('Draft storage is full. Save the source before leaving this page.');
    }
  };
  edit.oninput = () => {
    mark(edit.value !== current?.text);
    stash();
  };
  start.onclick = () => {
    if (!path && onOpen && current) {
      onOpen(current.path);
      return;
    }
    editing = true;
    edit.readOnly = false;
    start.hidden = true;
    save.hidden = cancel.hidden = false;
    edit.focus();
  };
  cancel.onclick = () => {
    editing = false;
    mark(false);
    stash();
    edit.readOnly = true;
    edit.value = current?.text || '';
    start.hidden = false;
    save.hidden = cancel.hidden = true;
    update();
  };
  save.onclick = async () => {
    if (!current) return;
    try {
      const r = await fetch('/yapnr/api/source-update/' + encodeURIComponent(project), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: current.path, text: edit.value, sha256: current.sha256 }),
      });
      const data = await r.json();
      if (!r.ok) throw Error(data.error || 'Source save failed');
      current = { ...current, text: edit.value, sha256: data.sha256 };
      mark(false);
      stash();
      label.textContent =
        'Working tree saved · ' +
        data.sha256.slice(0, 12) +
        ' · rendered artifacts require a new build';
      report?.('Source saved. Prior artifacts retain their original hashes.');
    } catch (error) {
      report?.(error.message);
    }
  };
  async function update() {
    if (disposed || pending) return;
    pending = true;
    try {
      const data = await snapshot(project);
      if (disposed) return;
      if (!selected && data.sources.length) selected = data.sources[0].path;
      const prior = chooser.value;
      chooser.replaceChildren(
        ...data.sources.map(file => {
          const o = el('option', file.path);
          o.value = file.path;
          return o;
        })
      );
      chooser.value = selected || prior;
      const source = data.sources.find(file => file.path === selected);
      if (!source) {
        label.textContent = 'No source mapping available for this view.';
        if (!dirty) edit.value = '';
        return;
      }
      if (!current && path) {
        try {
          const draft = JSON.parse(localStorage.getItem(draftKey()) || 'null');
          if (
            draft &&
            typeof draft.text === 'string' &&
            typeof draft.baseline === 'string' &&
            /^[a-f0-9]{64}$/.test(draft.sha256)
          ) {
            current = { ...source, text: draft.baseline, sha256: draft.sha256 };
            edit.value = draft.text;
            editing = true;
            edit.readOnly = false;
            start.hidden = true;
            save.hidden = cancel.hidden = false;
            mark(draft.text !== draft.baseline);
          }
        } catch {}
      }
      if (current && dirty && current.sha256 !== source.sha256) {
        label.textContent =
          'Working tree changed externally · unsaved buffer retained; save requires reconciliation';
        return;
      }
      if (!dirty) current = source;
      if (!dirty && edit.value !== source.text) {
        const scroll = edit.scrollTop,
          start = edit.selectionStart,
          end = edit.selectionEnd;
        edit.value = source.text;
        edit.scrollTop = scroll;
        if (editing) edit.setSelectionRange(start, end);
      }
      label.textContent = `${dirty ? 'Unsaved buffer over working tree' : 'Working tree'} · ${
        source.path
      } · ${source.sha256.slice(0, 12)}${
        data.source_newer ? ' · newer than exported netlist' : ''
      }`;
      if (data.schematic)
        label.textContent +=
          ' · netlist ' +
          data.schematic.sha256.slice(0, 12) +
          ' (source equivalence not independently recorded)';
    } catch (error) {
      label.textContent = error.message;
    } finally {
      pending = false;
    }
  }
  update();
  const timer = setInterval(update, 1000);
  return {
    discard: () => cancel.click(),
    dirty: () => dirty,
    dispose: () => {
      disposed = true;
      clearInterval(timer);
    },
    reveal(line) {
      edit.focus();
      const offset = edit.value
        .split('\n')
        .slice(0, Math.max(0, line - 1))
        .join('\n').length;
      edit.setSelectionRange(offset, offset);
      edit.scrollTop = Math.max(0, (line - 3) * 21);
    },
  };
}
