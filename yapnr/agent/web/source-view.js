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
export function mountSource(container, { project, path, onOpen, onDirty, onAsk, report } = {}) {
  container.replaceChildren();
  container.classList.add('source-view');
  const head = el('div'),
    label = el('p'),
    chooser = el('select'),
    edit = el('textarea'),
    actions = el('div'),
    code = el('pre');
  code.className = 'source-code';
  code.tabIndex = 0;
  code.setAttribute('aria-label', 'Highlighted Atopile source');
  edit.hidden = true;
  chooser.setAttribute('aria-label', 'Source file');
  edit.setAttribute('aria-label', 'Source text');
  edit.spellcheck = false;
  edit.readOnly = true;
  const start = el('button', 'Edit working tree'),
    save = el('button', 'Save source'),
    cancel = el('button', 'Discard edits'),
    ask = el('button', 'Ask about selection');
  ask.disabled = true;
  save.hidden = cancel.hidden = true;
  head.append(label, chooser);
  actions.append(start, save, cancel, ask);
  container.append(head, code, edit, actions);
  let current = null,
    dirty = false,
    editing = false,
    selected = path || '',
    disposed = false,
    pending = false;
  let files = [],
    nativeIndex = null,
    selection = null,
    selectionBuffer = '',
    rendered = '';
  const keywords = new Set(
    'module component interface new signal pin import from trait pragma to within assert is pass for in if True False None'.split(
      ' '
    )
  );
  function paint() {
    const key = selected + '\0' + edit.value + '\0' + Boolean(nativeIndex);
    if (key === rendered) return;
    rendered = key;
    code.replaceChildren();
    const imports = new Map();
    const normalize = value => new URL(value, 'https://source.invalid/').pathname.slice(1);
    edit.value.split('\n').forEach((text, index) => {
      const line = el('div');
      line.dataset.line = index + 1;
      const imported = /^\s*from\s+["']([^"']+)["']\s+import\s+(.+)/.exec(text);
      const builtin = /^\s*import\s+(.+)/.exec(text);
      if (builtin) for (const name of builtin[1].split(',')) imports.set(name.trim(), '@module');
      if (imported)
        for (const name of imported[2].split(',')) imports.set(name.trim(), imported[1]);
      const tokens =
        text.match(
          /#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b[A-Za-z_]\w*\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[A-Za-zµΩ%]*|[^A-Za-z_\d#"']+/g
        ) || [];
      for (const token of tokens) {
        let target = null;
        const module = nativeIndex?.modules?.[token];
        const ref = imports.get(token) || (imported && /^["']/.test(token) ? imported[1] : null);
        if (ref) {
          const candidates = [
            normalize(selected.split('/').slice(0, -1).join('/') + '/' + ref),
            normalize(ref),
          ];
          const local = candidates.find(p => files.some(f => f.path === p));
          if (local) target = { path: local };
          else if (module) target = { path: module.file, reference: true, line: module.line };
          else {
            const native = nativeIndex?.files?.find(
              f => f.path === ref || f.path.endsWith('/' + ref)
            );
            if (native) target = { path: native.path, reference: true };
          }
        }
        const span = el(target ? 'a' : 'span', token);
        span.className = token.startsWith('#')
          ? 'source-comment'
          : /^["']/.test(token)
            ? 'source-string'
            : keywords.has(token)
              ? 'source-keyword'
              : /^\d/.test(token)
                ? 'source-number'
                : '';
        if (target) {
          span.href = '#';
          span.title = 'Open ' + target.path;
          span.onclick = event => {
            event.preventDefault();
            onOpen?.(target.path, target.reference, target.line);
          };
        }
        line.append(span);
      }
      if (!text) line.append(document.createTextNode('\n'));
      code.append(line);
    });
  }
  function captureSelection() {
    let text = '',
      start = 1,
      end = 1;
    if (editing) {
      text = edit.value.slice(edit.selectionStart, edit.selectionEnd);
      start = edit.value.slice(0, edit.selectionStart).split('\n').length;
      end = edit.value.slice(0, edit.selectionEnd).split('\n').length;
    } else {
      const sel = window.getSelection();
      if (sel?.rangeCount && code.contains(sel.anchorNode) && code.contains(sel.focusNode)) {
        text = sel.toString();
        const line = node =>
          (node.nodeType === 1 ? node : node.parentElement)?.closest('[data-line]')?.dataset.line;
        start = Math.min(Number(line(sel.anchorNode)), Number(line(sel.focusNode)));
        end = Math.max(Number(line(sel.anchorNode)), Number(line(sel.focusNode)));
      }
    }
    selection = text
      ? {
          kind: 'source',
          file: selected,
          line: start,
          end,
          text,
          working_tree_sha256: current?.sha256,
          draft: dirty,
        }
      : null;
    selectionBuffer = edit.value;
    ask.disabled = !selection;
  }
  document.addEventListener('selectionchange', captureSelection);
  edit.addEventListener('select', captureSelection);
  ask.onpointerdown = e => e.preventDefault();
  ask.onclick = async () => {
    if (!selection) return;
    const context = structuredClone(selection),
      buffer = selectionBuffer;
    context.buffer_sha256 = [
      ...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(buffer))),
    ]
      .map(b => b.toString(16).padStart(2, '0'))
      .join('');
    onAsk?.(context);
  };
  fetch('/yapnr/api/source-index/' + encodeURIComponent(project))
    .then(r => (r.ok ? r.json() : null))
    .then(value => {
      if (!disposed) {
        nativeIndex = value;
        paint();
      }
    })
    .catch(() => {});
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
    captureSelection();
  };
  start.onclick = () => {
    if (!path && onOpen && current) {
      onOpen(current.path);
      return;
    }
    editing = true;
    code.hidden = true;
    edit.hidden = false;
    edit.readOnly = false;
    start.hidden = true;
    save.hidden = cancel.hidden = false;
    edit.focus();
  };
  cancel.onclick = () => {
    editing = false;
    code.hidden = false;
    edit.hidden = true;
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
      files = data.sources;
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
      let source = data.sources.find(file => file.path === selected);
      if (!source && selected) {
        const r = await fetch(
          '/yapnr/api/source-file/' +
            encodeURIComponent(project) +
            '?path=' +
            encodeURIComponent(selected)
        );
        const result = await r.json();
        if (!r.ok) throw Error(result.error || 'File unavailable');
        source = result;
      }
      start.hidden = !selected.endsWith('.ato') || editing;
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
            code.hidden = true;
            edit.hidden = false;
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
      if (!disposed) paint();
    }
  }
  const ready = update();
  const timer = setInterval(update, 1000);
  return {
    ready,
    discard: () => cancel.click(),
    dirty: () => dirty,
    dispose: () => {
      disposed = true;
      clearInterval(timer);
      document.removeEventListener('selectionchange', captureSelection);
    },
    reveal(line) {
      if (!editing) {
        code.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ block: 'center' });
        code.focus();
        return;
      }
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

export function mountSourceBrowser(container, { project, onOpen, report } = {}) {
  container.replaceChildren();
  container.classList.add('source-browser');
  const search = el('input'),
    tree = el('div');
  search.type = 'search';
  search.placeholder = 'Filter project files…';
  search.setAttribute('aria-label', 'Filter project files');
  container.append(search, tree);
  let files = [],
    key = '',
    disposed = false,
    pending = false;
  function render() {
    const opened = new Map(
      [...tree.querySelectorAll('details')].map(d => [d.dataset.path, d.open])
    );
    const root = {};
    for (const file of files.filter(f =>
      f.path.toLowerCase().includes(search.value.toLowerCase())
    )) {
      const parts = file.path.split('/');
      let node = root;
      for (const part of parts.slice(0, -1)) node = node[part] ||= {};
      node[parts.at(-1)] = file.path;
    }
    function branch(node, prefix = '') {
      const list = el('ul');
      for (const [name, item] of Object.entries(node).sort(
        ([a, x], [b, y]) => (typeof x === 'string') - (typeof y === 'string') || a.localeCompare(b)
      )) {
        const row = el('li');
        if (typeof item === 'string') {
          const button = el('button', name);
          button.title = item;
          button.onclick = () => onOpen?.(item);
          row.append(button);
        } else {
          const dir = el('details'),
            path = prefix + name + '/';
          dir.dataset.path = path;
          dir.open = search.value ? true : opened.get(path) ?? !prefix;
          dir.append(el('summary', name), branch(item, path));
          row.append(dir);
        }
        list.append(row);
      }
      return list;
    }
    tree.replaceChildren(files.length ? branch(root) : el('p', 'Waiting for project files.'));
  }
  search.oninput = render;
  async function update() {
    if (pending || disposed) return;
    pending = true;
    try {
      const data = await snapshot(project);
      if (disposed) return;
      const next = JSON.stringify(data.files || data.sources.map(f => ({ path: f.path })));
      if (next !== key) {
        key = next;
        files = JSON.parse(next);
        render();
      }
    } catch (error) {
      report?.(error.message);
    } finally {
      pending = false;
    }
  }
  update();
  const timer = setInterval(update, 2000);
  return {
    dispose() {
      disposed = true;
      clearInterval(timer);
    },
  };
}
