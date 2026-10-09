/* Workspace-native UI. OpenCode supplies the runtime API, never the page shell. */
import { Workbench } from '/yapnr/dock.js';
import { mountSource, mountSourceBrowser } from '/yapnr/source-view.js';
import { mountTiming } from '/yapnr/timing-view.js';
import { mountViewer, sendView } from '/yapnr/adapters.js';
import { mountTraceability } from '/yapnr/traceability.js';

(() => {
  const shadow = document;
  const $ = id =>
    id === 'body' && state.bodyTarget ? state.bodyTarget : document.getElementById(id);
  const state = {
    project: '',
    tab: 'artifacts',
    data: null,
    active: null,
    strokes: [],
    view: null,
    dirty: false,
    scratchDraft: null,
    revision: null,
    pending: false,
    threads: null,
    thread: null,
    threadDraft: null,
    main: {},
    mode: 'chat',
    newNote: { title: '', body: '' },
    sessionId: '',
    messagesKey: '',
    providers: null,
    chatDrafts: (() => {
      try {
        return JSON.parse(localStorage.getItem('yapnr.ask-drafts') || '{}');
      } catch {
        return {};
      }
    })(),
    bodyTarget: null,
    nativeViews: {},
    sourceViews: {},
    viewerFrames: {},
    generation: 0,
  };
  const say = text => {
    $('status').textContent = text;
  };
  const button = (text, action) => {
    const b = document.createElement('button');
    b.textContent = text;
    b.onclick = action;
    return b;
  };
  const node = (tag, text, className) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (className) n.className = className;
    return n;
  };
  const endpoint = path => `/yapnr/api/${path}/${encodeURIComponent(state.project)}`;
  const asset = id => `/yapnr/artifact/${encodeURIComponent(state.project)}/${id}`;
  const scene = id => `/yapnr/scene/${encodeURIComponent(state.project)}/${id}`;
  async function json(url, data, method = 'POST') {
    const r = await fetch(
      url,
      data === undefined
        ? {}
        : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }
    );
    const text = await r.text();
    let value;
    try {
      value = text ? JSON.parse(text) : null;
    } catch {
      throw Error(`Invalid server response (${r.status})`);
    }
    if (!r.ok) throw Error(value?.error?.message || value?.error || `Request failed (${r.status})`);
    return value;
  }
  const session = () => state.sessionId;
  const inferredProject = () => state.project;
  const chatPath = id => `/workspace/${state.project}/session/${id}`;
  const scope = () =>
    '?directory=' + encodeURIComponent(state.data?.agent_directory || '/projects/' + state.project);
  const legacyTypes = {
    artifacts: 'Artifacts',
    scratchpad: 'Design document',
    threads: 'Notes',
    requirements: 'Requirements & risks',
    activity: 'Activity',
  };
  const viewTitles = {
    board: 'Board',
    schematic: 'Schematic',
    three: '3D',
    source: 'Sources',
    'source-browser': 'Source browser',
    experiments: 'Experiments',
    controls: 'View controls',
    exploration: 'Exploration',
    ask: 'Ask',
    inspect: 'Inspect',
    notes: 'Notes',
    timing: 'Timing',
    activity: 'Activity',
    requirements: 'Requirements & risks',
    artifacts: 'Artifacts',
    scratchpad: 'Design document',
  };
  const wb = new Workbench($('shell'), {
    activate: activateView,
    announce: say,
    layout: l => {
      for (const name of ['conversation', 'inspect'])
        $(name + '-layout').setAttribute('aria-pressed', String(l.preset === name));
    },
    close: async ids => {
      if (
        ids.some(id => state.sourceViews[id]?.dirty()) &&
        !confirm('Discard unsaved source edits and close these views?')
      )
        return false;
      if (
        ids.includes('scratchpad') &&
        state.dirty &&
        !confirm('Discard unsaved document edits and close this view?')
      )
        return false;
      for (const id of ids) state.sourceViews[id]?.discard();
      if (ids.includes('scratchpad')) {
        state.dirty = false;
        delete wb.registry.get('scratchpad').element.dataset.rendered;
      }
      return true;
    },
  });
  for (const [id, title] of Object.entries(viewTitles)) {
    const element = id === 'ask' ? $('chat') : id === 'board' ? $('experiment') : node('div');
    const entry = wb.register({ id, title, type: id, scope: 'Project / working tree' }, element);
    if (legacyTypes[id] || id === 'notes') {
      element.classList.add('legacy-view');
      state.nativeViews[id] = entry;
    }
  }
  let presenceSession = '',
    presenceProject = '';
  const client = localStorage.getItem('yapnr.client') || crypto.randomUUID();
  localStorage.setItem('yapnr.client', client);
  async function lifecycle(action, project = presenceProject) {
    if (!project || !presenceSession) return;
    await json('/yapnr/api/presence/' + encodeURIComponent(project), {
      action,
      client,
      session: presenceSession,
    });
  }
  async function startPresence(project) {
    if (presenceProject) await lifecycle('close');
    presenceProject = project;
    presenceSession = crypto.randomUUID();
    await lifecycle('open');
  }
  setInterval(() => lifecycle('heartbeat').catch(() => {}), 15000);
  function activateView(id) {
    if (id === 'notes') id = 'threads';
    if (legacyTypes[id]) {
      const entry = wb.registry.get(id === 'threads' ? 'notes' : id);
      state.tab = id;
      state.bodyTarget = entry.element;
      if (
        id === 'requirements' ||
        entry.project !== state.project ||
        !entry.element.dataset.rendered
      ) {
        entry.project = state.project;
        entry.element.dataset.rendered = '1';
        render();
      }
    }
    if (id === 'source') ensureSource('source');
    if (id === 'timing' && !state.timingProject) {
      state.timingProject = state.project;
      state.disposeTiming = mountTiming(wb.registry.get('timing').element, state.project, {
        reveal: receipt => {
          const item = state.data?.artifacts.find(a => a.id === receipt);
          if (item) showArtifact(item);
          else {
            openLegacy('activity');
            say('Workflow receipt ' + receipt + ' is recorded in .yapnr/workflow/objects.');
          }
        },
        native: () => {
          wb.register({
            id: 'native-timing',
            title: 'Experiment timing',
            type: 'timing',
            scope: 'Native experiment statistics',
          });
          ensureViewer('native-timing');
          wb.open('native-timing');
        },
      });
    }
    if (['board', 'schematic', 'three', 'experiments', 'exploration', 'native-timing'].includes(id))
      ensureViewer(id);
    if (id === 'controls') requestControls();
  }
  function openLegacy(type, force = false) {
    const id = type === 'threads' ? 'notes' : type,
      entry = wb.registry.get(type) || wb.registry.get(id);
    if (force && entry) delete entry.element.dataset.rendered;
    wb.open(id);
    activateView(id);
  }
  function ensureSource(id, path) {
    const entry = wb.registry.get(id);
    if (!entry || (entry.project === state.project && state.sourceViews[id])) return;
    entry.project = state.project;
    entry.type = 'source';
    state.sourceViews[id] = mountSource(entry.element, {
      project: state.project,
      path,
      report: say,
      onOpen: (p, reference, line) => openSource(p, reference, line),
      onAsk: context => {
        state.selection = context;
        state.selectionScope = {
          project: state.project,
          file: context.file,
          working_tree_sha256: context.working_tree_sha256,
          buffer_sha256: context.buffer_sha256,
          draft: context.draft,
        };
        $('selection-context').textContent =
          context.file +
          ':' +
          context.line +
          '–' +
          context.end +
          (context.draft ? ' · unsaved draft' : '');
        wb.open('ask');
        $('prompt').focus();
      },
      onDirty: dirty => {
        entry.scope = (dirty ? 'Unsaved buffer' : 'Working tree') + (path ? ' / ' + path : '');
        wb.render();
      },
    });
  }
  function openSource(path, reference = false, line = 1) {
    if (
      (reference ||
        path.split('/').some(p => ['.ato', '.venv', 'venv', 'site-packages'].includes(p))) &&
      state.data?.experiment_url
    ) {
      const id = 'reference:' + state.project + ':' + path,
        url = new URL(state.data.experiment_url);
      url.searchParams.set('src', path + ':' + line);
      const entry = wb.register({
        id,
        title: path.split('/').pop(),
        type: 'source',
        project: state.project,
        scope: 'Read-only reference source',
        viewerUrl: url.href,
      });
      state.viewerFrames[id] = mountViewer(entry.element, url.href, 'source');
      wb.open(id);
      return;
    }

    const id = 'source:' + state.project + ':' + path;
    wb.register({
      id,
      title: path.split('/').pop(),
      path,
      type: 'source',
      project: state.project,
      scope: 'Working tree / ' + path,
    });
    ensureSource(id, path);
    wb.open(id, wb.layout.panes.source ? 'source' : undefined);
  }
  function requestControls() {
    const id =
      state.pinnedControls ||
      (wb.activeCentral ? wb.layout.panes[wb.activeCentral]?.active : 'board');
    const target = state.viewerFrames[id] || state.viewerFrames.board;
    state.controlsTarget = target;
    sendView(target, 'yapnr-view-controls');
  }
  function ensureViewer(id) {
    const entry = wb.registry.get(id);
    if (!entry) return;
    const mode = id === 'native-timing' ? 'timing' : id;
    state.viewerFrames[id] = mountViewer(
      entry.element,
      state.data?.experiment_url,
      mode,
      'Live / artifact hash unavailable'
    );
  }
  function mountProjectViews() {
    for (const id of ['board', 'schematic']) ensureViewer(id);
    for (const id of ['three', 'experiments', 'exploration', 'native-timing'])
      if (state.viewerFrames[id]) ensureViewer(id);
    ensureSource('source');
    if (state.sourceBrowserProject !== state.project) {
      state.sourceBrowser?.dispose();
      state.sourceBrowserProject = state.project;
      state.sourceBrowser = mountSourceBrowser(wb.registry.get('source-browser').element, {
        project: state.project,
        onOpen: p => openSource(p),
        report: say,
      });
    }
    const inspect = wb.registry.get('inspect').element;
    if (!inspect.childElementCount) {
      inspect.classList.add('inspection');
      inspect.append(
        node(
          'p',
          'Select an object in a linked engineering view. Highlights preserve the camera; Reveal navigates explicitly.'
        )
      );
    }
    for (const [id, entry] of wb.registry) {
      if (entry.project !== state.project) continue;
      if (id.startsWith('source:')) ensureSource(id, entry.path);
      if (entry.artifact) {
        const item = state.data.artifacts.find(a => a.id === entry.artifact);
        if (item) showArtifact(item, { open: false });
      }
      if (entry.viewerUrl && !state.viewerFrames[id])
        state.viewerFrames[id] = mountViewer(
          entry.element,
          entry.viewerUrl,
          id.startsWith('reference:') ? 'source' : 'board'
        );
    }
    for (const [id, entry] of Object.entries(state.nativeViews))
      if (!entry.element.hidden && entry.project !== state.project) activateView(id);
    activateView('timing');
  }
  function openPanel() {
    /* Native content belongs to its persistent dock tab. */
  }
  function switchView(mode) {
    if (mode === 'chat') {
      wb.dispatch({ type: 'preset', name: 'conversation' });
      return;
    }
    if (wb.layout.preset === 'conversation') wb.dispatch({ type: 'preset', name: 'inspect' });
    wb.open('board');
  }
  $('conversation-layout').onclick = () => wb.dispatch({ type: 'preset', name: 'conversation' });
  $('inspect-layout').onclick = () => wb.dispatch({ type: 'preset', name: 'inspect' });
  $('undo-layout').onclick = () => wb.undoLayout();
  $('view-menu').onclick = () => {
    const choices = $('view-choices');
    choices.replaceChildren();
    for (const [id, entry] of wb.registry)
      if (!entry.project || entry.project === state.project)
        choices.append(
          button(entry.title, () => {
            wb.open(id);
            activateView(id);
            $('views-dialog').close();
          })
        );
    choices.append(
      button('Conversation arrangement', () => {
        $('views-dialog').close();
        switchView('chat');
      }),
      button('Inspect arrangement', () => {
        $('views-dialog').close();
        switchView('experiment');
      }),
      button('Undo layout', () => wb.undoLayout()),
      button('Preferences', () => {
        $('views-dialog').close();
        $('preferences-dialog').showModal();
      }),
      button($('shell').hidden ? 'Reopen project' : 'Close project', () => {
        $('views-dialog').close();
        $('close-project').click();
      })
    );
    $('views-dialog').showModal();
  };
  $('reset-layout').onclick = () => {
    wb.dispatch({ type: 'reset' });
    $('views-dialog').close();
  };
  const theme = localStorage.getItem('yapnr.theme') || 'light';
  document.documentElement.dataset.theme = theme;
  function syncTheme() {
    const dark = document.documentElement.dataset.theme === 'dark';
    $('theme').textContent = dark ? 'Light' : 'Dark';
    $('brand').querySelector('img').src =
      '/yapnr/brand/assets/yapnr-mark-' + (dark ? 'dark' : 'light') + '.svg';
    for (const f of Object.values(state.viewerFrames))
      sendView(f, 'yapnr-theme', {
        theme: dark ? 'dark' : 'light',
        reduceTransparency: $('reduce-transparency').checked,
      });
  }
  $('theme').onclick = () => {
    document.documentElement.dataset.theme =
      document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('yapnr.theme', document.documentElement.dataset.theme);
    syncTheme();
  };
  $('preferences').onclick = () => $('preferences-dialog').showModal();
  $('reduce-transparency').checked = localStorage.getItem('yapnr.reduce-transparency') === 'true';
  document.documentElement.dataset.reduceTransparency = String($('reduce-transparency').checked);
  $('reduce-transparency').onchange = () => {
    document.documentElement.dataset.reduceTransparency = String($('reduce-transparency').checked);
    localStorage.setItem('yapnr.reduce-transparency', String($('reduce-transparency').checked));
    syncTheme();
  };
  $('prompt').addEventListener('input', () => {
    if (session()) {
      state.chatDrafts[state.project + ':' + session()] = $('prompt').value;
      localStorage.setItem('yapnr.ask-drafts', JSON.stringify(state.chatDrafts));
    }
  });
  $('close-project').onclick = async () => {
    if ($('shell').hidden) {
      await startPresence(state.project);
      $('shell').hidden = false;
      $('close-project').textContent = 'Close project';
      wb.render();
      return;
    }
    await lifecycle('close');
    presenceProject = '';
    say('Project closed in this window. Background work continues.');
    $('shell').hidden = true;
    $('close-project').textContent = 'Reopen project';
  };
  $('pending-actions').onclick = async () => {
    if ($('shell').hidden) await $('close-project').onclick();
    if (state.data?.workflow.state === 'requirements_review') openLegacy('requirements');
    else wb.open('ask');
  };
  syncTheme();
  async function setMain(id) {
    await json(endpoint('main-thread'), { session: id });
    state.main[state.project] = id;
  }
  async function refreshThreads() {
    const project = state.project;
    if (!project) return;
    try {
      const value = await json(endpoint('threads'));
      if (project !== state.project) return;
      state.threads = value;
      state.main[project] = value.main_thread || '';
      renderSidebar();
      if (state.tab === 'threads' && !state.thread && !state.threadDraft && !state.newNote.body)
        render();
    } catch (e) {
      say(e.message);
    }
  }
  function renderSidebar() {
    const list = $('thread-list');
    list.replaceChildren();
    for (const t of state.threads?.threads || []) {
      const b = button(t.title, () =>
        t.kind === 'opencode' ? selectSession(t.id) : (openLegacy('threads'), showThread(t))
      );
      b.className = 'thread-link' + (t.id === session() ? ' selected' : '');
      b.append(
        node(
          'small',
          t.kind === 'focused'
            ? 'Focused Ask'
            : t.id === state.main[state.project]
              ? 'Main conversation'
              : 'Conversation'
        )
      );
      list.append(b);
    }
    const registry = $('artifact-list');
    registry.replaceChildren();
    for (const a of [...(state.data?.artifacts || [])].reverse()) {
      const b = button(a.title, () => {
        state.tab = 'artifacts';
        openPanel();
        showArtifact(a);
      });
      b.className = 'artifact-link';
      b.append(node('small', a.kind));
      registry.append(b);
    }
  }
  async function refresh() {
    if (!state.project || state.pending === state.project) return;
    const project = state.project;
    state.pending = project;
    try {
      const data = await json(endpoint('project'));
      if (project !== state.project) return;
      state.data = data;
      $('stage').textContent =
        data.workflow.revision === undefined
          ? ''
          : `Revision ${data.workflow.revision}${
              data.workflow.speculative ? ' · pending acceptance' : ''
            }`;
      renderWorkflow(data.workflow, data.harness);
      $('recording').textContent = `Recording: ${data.recording}`;
      renderSidebar();
      if (!state.dirty && state.tab === 'scratchpad' && !state.active) {
        const edit = $('scratch');
        if (edit && document.activeElement !== edit) {
          edit.value = data.scratchpad.text;
          state.revision = data.scratchpad.revision;
        }
      }
      mountProjectViews();
      $('pending-actions').hidden =
        data.workflow.state !== 'requirements_review' && !$('requests').childElementCount;
      $('pending-actions').textContent =
        data.workflow.state === 'requirements_review'
          ? 'Requirements review'
          : 'Agent needs your input';
    } catch (e) {
      say(e.message);
    } finally {
      if (state.pending === project) state.pending = false;
    }
  }
  async function attachNote(note) {
    const id = state.main[state.project];
    if (!id) {
      say('Choose a main OpenCode thread first.');
      return false;
    }
    try {
      const model = $('model').value.split('::');
      await json(endpoint('attach-note'), {
        session: id,
        note: note.id,
        revision: note.rev,
        ...($('model').value ? { model: { providerID: model[0], modelID: model[1] } } : {}),
      });
      say(
        `Attached ${note.id} to the main thread. Send a message there when ready for the agent to act.`
      );
      return true;
    } catch (e) {
      say(e.message);
      return false;
    }
  }
  async function showThread(thread) {
    try {
      const value = await json(endpoint('thread'), { kind: thread.kind, id: thread.id });
      state.thread = value;
      state.threadDraft = null;
      render();
    } catch (e) {
      say(e.message);
    }
  }
  function renderThreads(body) {
    const listing = state.threads;
    if (!listing) {
      body.append(node('p', 'Loading shared conversations…'));
      return;
    }
    const main = node('select');
    main.setAttribute('aria-label', 'Main OpenCode thread');
    const empty = node('option', 'Choose main thread');
    empty.value = '';
    main.append(empty);
    for (const t of listing.threads.filter(t => t.kind === 'opencode')) {
      const option = node('option', t.title);
      option.value = t.id;
      main.append(option);
    }
    main.value = state.main[state.project] || '';
    main.onchange = async () => {
      try {
        await setMain(main.value);
        say('Main thread selected. Notes can now be attached to it.');
      } catch (e) {
        say(e.message);
      }
    };
    body.append(node('label', 'Main conversation'), main);
    if (session() && inferredProject() === state.project)
      body.append(
        button('Use current Chat as main', async () => {
          try {
            await setMain(session());
            render();
          } catch (e) {
            say(e.message);
          }
        })
      );
    if (state.main[state.project]) {
      const link = node('a', 'Open main conversation');
      link.href = chatPath(state.main[state.project]);
      link.onclick = e => {
        e.preventDefault();
        selectSession(state.main[state.project]);
      };
      body.append(link);
    }
    if (state.thread) {
      const t = state.thread;
      body.append(
        button('← All threads', () => {
          if (state.threadDraft?.body && !confirm('Discard the unsaved note draft?')) return;
          state.thread = null;
          state.threadDraft = null;
          render();
        }),
        node('h2', t.title)
      );
      if (t.kind === 'opencode') {
        const link = node('a', 'Open this conversation in Chat');
        link.href = chatPath(t.id);
        link.onclick = e => {
          e.preventDefault();
          selectSession(t.id);
        };
        body.append(link);
      }
      if (t.kind === 'focused' && state.data?.experiment_url)
        body.append(
          button('Continue in Experiment Ask', () => {
            const id = 'focused:' + state.project + ':' + t.id;
            const entry = wb.register({
              id,
              title: 'Focused Ask · ' + t.title,
              type: 'ask',
              project: state.project,
              scope: 'Focused thread / ' + t.id,
            });
            const frame = mountViewer(entry.element, state.data.experiment_url, 'ask');
            wb.open(id, 'right');
            const send = () =>
              frame.contentWindow.postMessage(
                { type: 'yapnr-open-thread', session: t.id },
                new URL(frame.src).origin
              );
            if (frame.dataset.loaded) send();
            else
              frame.addEventListener(
                'load',
                () => {
                  frame.dataset.loaded = '1';
                  send();
                },
                { once: true }
              );
          })
        );
      if (t.kind === 'focused')
        for (const row of t.turns) {
          const card = node('div', undefined, 'card thread-turn');
          card.append(
            node('strong', row.message || 'Question'),
            node('p', row.answer ?? row.text ?? row.result ?? '')
          );
          if (row.selection?.length)
            card.append(node('pre', JSON.stringify(row.selection, null, 2)));
          if (row.tools?.length) {
            const details = node('details');
            details.append(
              node('summary', 'Tool calls'),
              node('pre', JSON.stringify(row.tools, null, 2))
            );
            card.append(details);
          }
          body.append(card);
        }
      else
        for (const message of t.messages) {
          const card = node('div', undefined, 'card thread-turn');
          card.append(node('strong', message.info.role));
          for (const part of message.parts || [])
            if (part.type === 'text') card.append(node('p', part.text));
            else if (part.type === 'tool') {
              const detail = node('details');
              detail.append(
                node('summary', part.tool || 'Tool call'),
                node('pre', JSON.stringify(part, null, 2))
              );
              card.append(detail);
            }
          body.append(card);
        }
      const draft = state.threadDraft || { title: t.title.slice(0, 120), body: '' };
      state.threadDraft = draft;
      const title = node('input');
      title.value = draft.title;
      title.maxLength = 120;
      title.setAttribute('aria-label', 'Note title');
      title.oninput = () => (draft.title = title.value);
      const edit = node('textarea');
      edit.value = draft.body;
      edit.placeholder = 'Findings or refinement to carry into the design conversation';
      edit.maxLength = 8000;
      edit.oninput = () => (draft.body = edit.value);
      body.append(node('label', 'Elevate through a note'), title, edit);
      const save = async elevate => {
        if (!draft.body.trim()) {
          say('Write the finding to preserve in the note.');
          return;
        }
        try {
          const note = await json(endpoint('note'), {
            title: draft.title,
            body: draft.body,
            thread: { kind: t.kind, id: t.id },
          });
          draft.body = '';
          edit.value = '';
          await refreshThreads();
          if (elevate) await attachNote(note);
          else say(`Saved ${note.id} in the shared workspace notes.`);
        } catch (e) {
          say(e.message);
        }
      };
      const actions = node('div', undefined, 'actions');
      actions.append(
        button('Save note', () => save(false)),
        button('Save & attach to main', () => save(true))
      );
      body.append(actions);
    } else {
      body.append(
        node(
          'p',
          'Focused Ask chats and OpenCode sessions share this project. Attach a note to bring findings into the main conversation.',
          'muted'
        )
      );
      for (const t of listing.threads) {
        const card = node('div', undefined, 'card');
        card.append(
          button(t.title, () => showThread(t)),
          node(
            'div',
            `${t.kind === 'focused' ? 'Experiment Ask' : 'OpenCode'}${
              t.id === state.main[state.project] ? ' · main' : ''
            }`,
            'muted'
          )
        );
        body.append(card);
      }
      const title = node('input');
      title.placeholder = 'Note title';
      title.maxLength = 120;
      title.value = state.newNote.title;
      title.oninput = () => (state.newNote.title = title.value);
      const edit = node('textarea');
      edit.placeholder = 'Attach a note to the current conversation';
      edit.maxLength = 8000;
      edit.value = state.newNote.body;
      edit.oninput = () => (state.newNote.body = edit.value);
      body.append(
        node('label', 'New conversation note'),
        title,
        edit,
        button('Save & attach to current Chat', async () => {
          if (!session() || inferredProject() !== state.project) {
            say('Open a Chat in this project first.');
            return;
          }
          try {
            const note = await json(endpoint('note'), {
              title: title.value,
              body: edit.value,
              thread: { kind: 'opencode', id: session() },
            });
            await setMain(session());
            await attachNote(note);
            title.value = '';
            edit.value = '';
            state.newNote = { title: '', body: '' };
            await refreshThreads();
          } catch (e) {
            say(e.message);
          }
        })
      );
    }
    body.append(node('h2', 'Shared notes'));
    for (const note of [...listing.notes].reverse()) {
      const card = node('div', undefined, 'card thread-note');
      card.append(
        node('strong', `${note.id} · ${note.title}`),
        node('div', `${note.status} · revision ${note.rev}`, 'muted'),
        node('p', note.body),
        button('Attach to main conversation', () => attachNote(note))
      );
      body.append(card);
    }
  }
  async function review(text, artifact, main = false, attachments = []) {
    const id = main ? state.main[state.project] || session() : session();
    if (!id) {
      say('Open an existing project chat before sending a review request.');
      return;
    }
    try {
      if (!$('model').value) throw Error('Select a connected model before sending a review.');
      const [providerID, modelID] = $('model').value.split('::');
      await json(endpoint('review'), {
        session: id,
        text,
        artifact,
        attachments,
        model: { providerID, modelID },
      });
      say('Sent to the agent for review.');
    } catch (e) {
      say(e.message);
    }
  }
  function reviewControls(container, items) {
    const entries = items
      .map(item => ({ item, review: state.data.reviews?.items[item.id] }))
      .filter(x => x.review && x.review.status !== 'superseded');
    if (!entries.length) return;
    const panel = node('section', undefined, 'artifact-review'),
      open = entries.flatMap(x => x.review.unresolved || []);
    panel.append(
      node('h3', 'Review requested'),
      node(
        'p',
        entries.map(x => `${x.item.title}: ${x.review.status.replaceAll('_', ' ')}`).join(' · ')
      )
    );
    const approve = button('Approve', async () => {
      try {
        await json(endpoint('approve'), { artifacts: entries.map(x => x.item.id) });
        await refresh();
        panel.remove();
        reviewControls(container, items);
        if (state.data.workflow.state === 'schematic') switchView('experiment');
        if (
          state.data.workflow.state === 'schematic' &&
          state.data.workflow.accepted_revision === state.data.workflow.revision &&
          state.main[state.project]
        )
          await json(endpoint('harness-resume'), { session: state.main[state.project] });
        say('Approved this content revision.');
      } catch (e) {
        say(e.message);
      }
    });
    approve.className = 'approve-artifact';
    approve.disabled = !!open.length || entries.every(x => x.review.status === 'approved');
    panel.append(approve);
    const feedback = button('Review Feedback', async () => {
      const notes = open.map(n => `${n.id} (${n.kind}): ${n.body}`).join('\n\n');
      await review(
        `Review the feedback on artifact(s) ${entries
          .map(x => x.item.id)
          .join(
            ', '
          )}.\n\n${notes}\n\nDiscuss all user questions in chat before proceeding; do not infer answers or acceptance. Update the YAML requirements/risk model or affected artifact, reconcile dependent evidence through the controller, publish the changed artifact and request a fresh review. Do not mark user questions resolved or approvals on the user's behalf.`,
        entries[0].item.id,
        true,
        entries.flatMap(x => x.review.annotations || [])
      );
    });
    feedback.disabled = !open.length;
    panel.append(feedback);
    for (const { item, review: record } of entries) {
      for (const note of record.feedback || []) {
        const card = node('div', undefined, 'review-note');
        card.append(
          node('strong', `${note.id} · ${note.kind} · ${note.status}`),
          node('p', note.body)
        );
        if ((record.unresolved || []).some(n => n.id === note.id))
          card.append(
            button('Mark addressed', async () => {
              try {
                await json(endpoint('resolve-feedback'), {
                  artifact: item.id,
                  note: note.id,
                  revision: note.rev,
                });
                await refresh();
                panel.remove();
                reviewControls(container, items);
              } catch (e) {
                say(e.message);
              }
            })
          );
        panel.append(card);
      }
    }
    const input = node('textarea');
    input.placeholder = 'Add feedback or a question about this review';
    input.setAttribute('aria-label', input.placeholder);
    const question = node('input');
    question.type = 'checkbox';
    const label = node('label', 'This is a question to discuss in chat');
    label.prepend(question);
    panel.append(
      input,
      label,
      button('Add review note', async () => {
        try {
          await json(endpoint('artifact-feedback'), {
            artifact: entries[0].item.id,
            text: input.value,
            question: question.checked,
          });
          await refresh();
          panel.remove();
          reviewControls(container, items);
        } catch (e) {
          say(e.message);
        }
      })
    );
    container.append(panel);
  }
  function render() {
    const body = $('body');
    body.hidden = false;
    body.replaceChildren();
    if (!state.data) return;
    $('view-title').textContent = {
      threads: 'Conversations & notes',
      artifacts: 'Artifacts',
      scratchpad: 'Design document',
      activity: 'Workflow',
      requirements: 'Requirements & risk review',
      experiment: 'Experiment',
    }[state.tab];
    if (state.tab === 'experiment') {
      switchView('experiment');
      return;
    }
    if (state.tab === 'requirements') {
      const article = state.project;
      state.active = null;
      body.append(node('p', 'Loading requirements model…'));
      json(endpoint('requirements'))
        .then(data => {
          if (state.project === article) {
            mountTraceability(body, data, {
              openArtifact: showArtifact,
              ask: text => {
                wb.open('ask');
                $('prompt').value = text;
                $('prompt').focus();
              },
              select: selection => {
                state.selection = selection;
                $('selection-context').textContent = selection.ref;
              },
            });
            const pending = state.data.artifacts.filter(
              a =>
                state.data.reviews?.items[a.id]?.stage === 'requirements_review' &&
                state.data.reviews.items[a.id].status !== 'superseded'
            );
            const controls = node('div');
            reviewControls(controls, pending);
            controls.append(button('Reload YAML model', () => openLegacy('requirements', true)));
            body.prepend(controls);
          }
        })
        .catch(e => say(e.message));
      return;
    }
    if (state.tab === 'threads') {
      renderThreads(body);
      return;
    }
    if (state.tab === 'scratchpad') {
      state.active = null;
      const edit = node('textarea');
      edit.id = 'scratch';
      edit.setAttribute('aria-label', 'Shared design scratchpad');
      edit.value = state.dirty ? state.scratchDraft : state.data.scratchpad.text;
      if (!state.dirty) state.revision = state.data.scratchpad.revision;
      edit.oninput = () => {
        state.scratchDraft = edit.value;
        state.dirty = true;
        say('Unsaved user refinement');
      };
      body.append(
        node(
          'p',
          'The agent and user share this document. Edits are refinements, not automatic requirement acceptance.',
          'muted'
        ),
        edit
      );
      const actions = node('div', undefined, 'actions');
      actions.append(
        button('Save revision', async () => {
          try {
            const result = await json(endpoint('scratchpad'), {
              text: edit.value,
              revision: state.revision,
            });
            state.revision = result.revision;
            state.dirty = false;
            state.data.scratchpad = { text: edit.value, revision: result.revision };
            say('Saved. The edit and previous revision are preserved.');
          } catch (e) {
            say(e.message);
          }
        }),
        button('Ask agent to reconcile edits', () =>
          review(
            `Review and reconcile the current shared design document at reports/design.md. Incorporate user refinements, update requirements/risk analysis as appropriate, and use the workflow controller to invalidate and rerun affected stages. User edits do not by themselves approve requirements.`
          )
        )
      );
      body.append(actions);
      return;
    }
    if (state.tab === 'activity') {
      body.append(node('pre', JSON.stringify(state.data.workflow, null, 2)));
      body.append(
        node(
          'p',
          'Full conversation and tool events: .yapnr/workspace/conversation.jsonl. Workspace export preserves that record.',
          'muted'
        )
      );
      return;
    }
    body.append(
      node(
        'p',
        `${state.data.artifacts.length} preserved artifacts. Select one to inspect or annotate.`,
        'muted'
      )
    );
    for (const item of [...state.data.artifacts].reverse()) {
      const card = node('div', undefined, 'card');
      card.append(
        button(item.title, () => showArtifact(item)),
        node(
          'div',
          `${item.kind} · ${
            state.data.reviews?.items[item.id]?.status || 'draft'
          } · ${item.id.slice(0, 12)}`,
          'muted'
        )
      );
      body.append(card);
    }
  }
  async function showArtifact(item, { open = true } = {}) {
    const id = 'artifact:' + state.project + ':' + item.id;
    const entry = wb.register({
      id,
      title: item.title,
      type: item.kind,
      project: state.project,
      artifact: item.id,
      scope: 'Artifact / ' + item.id,
    });
    if (open) {
      wb.open(id);
      state.active = item;
    }
    const body = entry.element;
    if (body.dataset.rendered) return;
    body.dataset.rendered = '1';
    body.replaceChildren(
      button('← Artifacts', () => {
        state.active = null;
        openLegacy('artifacts');
      }),
      node('h2', item.title),
      node('div', item.id, 'muted')
    );
    reviewControls(body, [item]);
    const view = node('div', undefined, 'view');
    body.append(view);
    const actions = node('div', undefined, 'actions');
    body.append(actions);
    const download = node('a', 'Download original');
    download.href = asset(item.id);
    download.download = item.source_path?.split('/').pop() || item.id;
    actions.append(download);
    if (item.kind === 'scene') {
      const frame = node('iframe');
      frame.src = scene(item.id);
      frame.title = item.title;
      frame.sandbox = 'allow-scripts';
      frame.dataset.yapnrArtifact = item.id;
      view.append(frame);
      const capture = button('Capture view for markup', () =>
        frame.contentWindow.postMessage({ type: 'yapnr-snapshot' }, '*')
      );
      capture.dataset.sceneCapture = item.id;
      capture.disabled = true;
      actions.append(capture);
      return;
    }
    if (item.metadata?.viewer_url) {
      const frame = node('iframe');
      frame.src = item.metadata.viewer_url;
      frame.title = item.title;
      view.append(frame);
    }
    const suffix = item.path.split('.').pop().toLowerCase();
    if (['png', 'jpg', 'jpeg', 'webp', 'svg'].includes(suffix))
      return imageEditor(view, actions, asset(item.id), item);
    if (item.metadata?.preview_id)
      return imageEditor(view, actions, asset(item.metadata.preview_id), item);
    try {
      const response = await fetch(asset(item.id));
      const text = await response.text();
      view.append(node('pre', text.slice(0, 200000)));
      if (text.length > 200000)
        view.append(
          node('p', 'Preview shortened; download contains the complete artifact.', 'muted')
        );
    } catch (e) {
      say(e.message);
    }
  }
  function imageEditor(view, actions, url, item, capturedView = null) {
    const strokes = [];
    const project = state.project;
    const image = new Image();
    image.onload = () => {
      if (!view.isConnected || project !== state.project) return;
      const canvas = node('canvas');
      const scale = Math.min(1, 1600 / image.width, 1600 / image.height);
      canvas.width = Math.max(1, Math.round(image.width * scale));
      canvas.height = Math.max(1, Math.round(image.height * scale));
      const ctx = canvas.getContext('2d');
      view.replaceChildren(canvas);
      ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      let drawing = null;
      const position = event => {
        const rect = canvas.getBoundingClientRect();
        return [
          Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
          Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
        ];
      };
      const redraw = () => {
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
        ctx.strokeStyle = '#ff4545';
        ctx.lineWidth = 3;
        ctx.lineCap = 'round';
        for (const points of strokes) {
          ctx.beginPath();
          points.forEach(([x, y], i) =>
            i
              ? ctx.lineTo(x * canvas.width, y * canvas.height)
              : ctx.moveTo(x * canvas.width, y * canvas.height)
          );
          ctx.stroke();
        }
      };
      canvas.onpointerdown = event => {
        canvas.setPointerCapture(event.pointerId);
        drawing = [position(event)];
        strokes.push(drawing);
      };
      canvas.onpointermove = event => {
        if (drawing) {
          drawing.push(position(event));
          redraw();
        }
      };
      canvas.onpointerup = canvas.onpointercancel = () => {
        drawing = null;
      };
      const note = node('textarea');
      note.placeholder = 'Describe the issue or refinement for the agent';
      view.append(node('label', 'Review note'), note);
      actions.append(
        button('Undo mark', () => {
          strokes.pop();
          redraw();
        }),
        button('Save annotation', async () => {
          try {
            const annotated = await json(endpoint('annotation'), {
              artifact: item.id,
              image: canvas.toDataURL('image/png'),
              strokes: strokes,
              view: capturedView,
              note: note.value,
            });
            state.savedAnnotation = annotated;
            say('Annotation saved with its original artifact and view.');
          } catch (e) {
            say(e.message);
          }
        }),
        button('Send marked view to agent', async () => {
          try {
            const annotated = await json(endpoint('annotation'), {
              artifact: item.id,
              image: canvas.toDataURL('image/png'),
              strokes: strokes,
              view: capturedView,
              note: note.value,
            });
            await review(
              `Review the annotated artifact ${annotated.id}, based on original ${item.id}. User note: ${note.value}. Incorporate refinements and use workflow transitions/evidence before accepting or rerunning affected work.`,
              annotated.id
            );
          } catch (e) {
            say(e.message);
          }
        })
      );
    };
    image.onerror = () => say('Artifact image could not load.');
    image.src = url;
  }
  addEventListener('message', event => {
    const value = event.data;
    if (!['yapnr-ready', 'yapnr-snapshot'].includes(value?.type)) return;
    const entry = wb.registry.get('artifact:' + state.project + ':' + value.artifact);
    const frame = entry?.element.querySelector('iframe[data-yapnr-artifact]');
    const item = state.data?.artifacts.find(a => a.id === value.artifact);
    if (!frame || event.source !== frame.contentWindow || !item) return;
    if (value.type === 'yapnr-ready') {
      entry.element.querySelector('[data-scene-capture]').disabled = false;
      return;
    }
    if (!value.image?.startsWith('data:image/png;base64,'))
      return say('Scene has no valid render yet.');
    imageEditor(
      entry.element.querySelector('.view'),
      entry.element.querySelector('.actions'),
      value.image,
      item,
      value.view
    );
  });

  function inlineArtifact(id, container) {
    const item = state.data?.artifacts.find(a => a.id === id);
    if (!item) {
      container.append(
        node('p', `Artifact ${id.slice(0, 12)} is not published in this workspace.`, 'muted')
      );
      return;
    }
    const card = node('section', undefined, 'inline-artifact');
    card.dataset.yapnrInlineArtifact = id;
    card.append(node('strong', item.title));
    if (item.kind === 'scene') {
      const f = node('iframe');
      f.src = scene(id);
      f.title = item.title;
      f.sandbox = 'allow-scripts';
      card.append(f);
    } else if (/\.(png|jpe?g|webp|svg)$/i.test(item.path)) {
      const img = node('img');
      img.src = asset(id);
      img.alt = item.title;
      card.append(img);
    }
    card.append(
      button('Open / annotate', () => {
        state.tab = 'artifacts';
        openPanel();
        showArtifact(item);
      })
    );
    container.append(card);
  }
  function inlineText(text, target) {
    const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^\s)]+)\))/g;
    let cursor = 0;
    for (const m of text.matchAll(pattern)) {
      target.append(document.createTextNode(text.slice(cursor, m.index)));
      if (m[2]) target.append(node('strong', m[2]));
      else if (m[3]) target.append(node('code', m[3]));
      else {
        let url;
        try {
          url = new URL(m[5], location.href);
        } catch {}
        if (url && ['http:', 'https:'].includes(url.protocol)) {
          const a = node('a', m[4]);
          a.href = url.href;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          target.append(a);
        } else target.append(document.createTextNode(m[0]));
      }
      cursor = m.index + m[0].length;
    }
    target.append(document.createTextNode(text.slice(cursor)));
  }
  function markdown(text, container) {
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.startsWith('```')) {
        const code = [];
        while (++i < lines.length && !lines[i].startsWith('```')) code.push(lines[i]);
        const pre = node('pre');
        pre.append(node('code', code.join('\n')));
        container.append(pre);
        continue;
      }
      if (/^\s*\|.+\|\s*$/.test(line) && /^\s*\|?\s*:?-{3}/.test(lines[i + 1] || '')) {
        const table = node('table');
        const cells = value => value.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
        const header = node('tr');
        for (const cell of cells(line)) {
          const th = node('th');
          inlineText(cell.trim(), th);
          header.append(th);
        }
        table.append(header);
        i++;
        while (/^\s*\|.+\|\s*$/.test(lines[i + 1] || '')) {
          const row = node('tr');
          for (const cell of cells(lines[++i])) {
            const td = node('td');
            inlineText(cell.trim(), td);
            row.append(td);
          }
          table.append(row);
        }
        const wrap = node('div', undefined, 'table-scroll');
        wrap.append(table);
        container.append(wrap);
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)/);
      const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
      const out = node(
        heading ? 'h' + heading[1].length : bullet ? 'div' : 'div',
        undefined,
        'message-text'
      );
      inlineText(heading ? heading[2] : bullet ? '• ' + bullet[1] : line, out);
      if (!line) out.append(node('br'));
      container.append(out);
    }
  }
  function richText(text, container) {
    const pattern = /\[yapnr-(?:artifact|visual):([0-9a-f]{64})\]/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      markdown(text.slice(cursor, match.index), container);
      inlineArtifact(match[1], container);
      cursor = match.index + match[0].length;
    }
    markdown(text.slice(cursor), container);
  }
  function renderMessages(messages, status = 'idle') {
    const area = $('messages');
    const nearBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 100;
    const oldScroll = area.scrollTop;
    const expanded = new Set(
      [...area.querySelectorAll('details[open][data-part]')].map(n => n.dataset.part)
    );
    area.replaceChildren();
    if (!messages.length && status === 'idle')
      area.append(
        node(
          'div',
          'Describe your design. The agent should begin with requirements and risk analysis.',
          'empty'
        )
      );
    const batches = [];
    for (const message of messages) {
      if (message.info.role === 'user' && !message.parts?.some(p => p.metadata?.yapnr_harness)) {
        batches.push({ user: message });
      } else if (message.info.role === 'assistant') {
        let batch = batches.at(-1);
        if (!batch || batch.user) {
          batch = { assistant: [] };
          batches.push(batch);
        }
        batch.assistant.push(message);
      }
    }
    if (status !== 'idle' && !batches.at(-1)?.assistant) batches.push({ assistant: [] });
    for (const [index, batch] of batches.entries()) {
      const card = node('article', undefined, 'message ' + (batch.user ? 'user' : 'assistant'));
      card.append(node('header', batch.user ? 'You' : 'Agent'));
      const tools = [],
        reasoning = [];
      for (const message of batch.user ? [batch.user] : batch.assistant) {
        card.dataset.message ||= message.info.id;
        for (const part of message.parts || []) {
          if (part.type === 'text') richText(part.text || '', card);
          else if (part.type === 'tool') tools.push(part);
          else if (part.type === 'reasoning' && part.text) reasoning.push(part);
          else if (part.type === 'file') {
            const u = part.url || '';
            if (
              /^(data:image\/(png|jpeg|webp|gif);base64,|\/yapnr\/artifact\/|https?:\/\/)/.test(
                u
              ) &&
              part.mime?.startsWith('image/')
            ) {
              const img = node('img');
              img.src = u;
              img.alt = part.filename || 'Generated image';
              img.loading = 'lazy';
              card.append(img);
            } else card.append(node('p', `Attachment: ${part.filename || part.mime}`));
          }
        }
        if (message.info.error)
          card.append(node('pre', JSON.stringify(message.info.error), 'error'));
      }
      const busy = !batch.user && index === batches.length - 1 && status !== 'idle';
      if (tools.length || reasoning.length) {
        const rollup = node('details', undefined, 'tool-rollup');
        rollup.dataset.part = (card.dataset.message || 'pending') + ':activity';
        rollup.open = expanded.has(rollup.dataset.part);
        const summary = node('summary');
        const spinner = node('span', undefined, busy ? 'loading-chiral' : 'activity-dot');
        spinner.setAttribute('aria-hidden', 'true');
        summary.append(
          spinner,
          node(
            'span',
            `${busy ? 'Working' : 'Activity'}${
              tools.length
                ? ' · ' + tools.length + ' tool call' + (tools.length === 1 ? '' : 's')
                : ''
            }${tools.some(t => t.state?.status === 'error') ? ' · errors recorded' : ''}`
          )
        );
        rollup.append(summary);
        let populated = false;
        const populate = () => {
          if (populated || !rollup.open) return;
          populated = true;
          for (const part of [...tools, ...reasoning]) {
            const d = node('details', undefined, part.type === 'tool' ? 'tool' : 'reasoning');
            d.dataset.part = part.id;
            d.open = expanded.has(part.id);
            d.append(
              node(
                'summary',
                part.type === 'tool' ? `${part.tool} · ${part.state?.status || ''}` : 'Reasoning'
              )
            );
            let filled = false;
            const fill = () => {
              if (!d.open || filled) return;
              filled = true;
              d.append(
                part.type === 'tool'
                  ? node('pre', JSON.stringify(part.state, null, 2))
                  : node('div', part.text, 'message-text')
              );
            };
            d.addEventListener('toggle', fill);
            fill();
            rollup.append(d);
          }
        };
        rollup.addEventListener('toggle', populate);
        populate();
        card.append(rollup);
      } else if (busy) {
        const loading = node('div', undefined, 'agent-loading');
        const spinner = node('span', undefined, 'loading-chiral');
        spinner.setAttribute('aria-hidden', 'true');
        loading.append(spinner, node('span', 'Working…'));
        loading.setAttribute('role', 'status');
        card.append(loading);
      }
      area.append(card);
    }
    area.scrollTop = nearBottom ? area.scrollHeight : oldScroll;
  }
  function renderWorkflow(workflow, harness) {
    const stages = [
      ['requirements_capture', 'Requirements'],
      ['requirements_review', 'Review'],
      ['schematic', 'Schematic'],
      ['placement_routing', 'Place & route'],
      ['verification', 'Verify'],
      ['complete', 'Complete'],
    ];
    const stopped = ['blocked', 'exhausted', 'cancelled'].includes(workflow.state);
    const effective = stopped ? workflow.resume_state : workflow.state;
    const rework = ['fixup', 'engine_repair'].includes(effective);
    const phase = rework ? 'schematic' : effective;
    const index = stages.findIndex(([key]) => key === phase);
    const bar = $('workflow-bar');
    bar.replaceChildren();
    for (const [i, [key, label]] of stages.entries()) {
      const step = node(
        'span',
        label,
        'workflow-step' + (i === index ? ' current' : i < index ? ' reached' : '')
      );
      step.dataset.stage = key;
      if (i === index) step.setAttribute('aria-current', 'step');
      bar.append(step);
    }
    const labels = {
      not_initialized: 'Workflow not initialized',
      requirements_capture: 'Capturing requirements & risks',
      requirements_review: 'Awaiting requirements & risk review',
      schematic: 'Schematic & part selection',
      placement_routing: 'Placement & routing',
      verification: 'Verification',
      fixup: 'Rework',
      engine_repair: 'Engine repair',
      complete: 'Complete · ready for PCB review',
      blocked: 'Blocked',
      exhausted: 'Methods exhausted',
      cancelled: 'Cancelled',
    };
    const label = labels[workflow.state] || 'Workflow state unavailable';
    const statuses = {
      budget_reached: 'Continuation budget reached',
      waiting_question: 'Awaiting your answer',
      waiting_permission: 'Awaiting permission',
      waiting_artifact_review: 'Awaiting artifact review',
      delivery_uncertain: 'Awaiting delivery confirmation',
      model_error: 'Provider error · retrying automatically',
      stopped: 'Automation stopped',
      turn_timeout: 'Agent turn timed out',
      connection_error: 'Agent connection error · reconnecting',
      resume_required: 'Imported workspace · continuation requires confirmation',
    };
    const budget = statuses[harness?.status] ? ' · ' + statuses[harness.status] : '';
    $('resume-harness').hidden =
      !statuses[harness?.status] ||
      harness?.enabled ||
      ['requirements_review', 'complete', 'blocked', 'exhausted', 'cancelled'].includes(
        workflow.state
      ) ||
      !session() ||
      session() !== state.main[state.project];
    const speculative =
      workflow.speculative &&
      ['schematic', 'placement_routing', 'verification', 'fixup', 'engine_repair'].includes(
        workflow.state
      )
        ? ' · speculative'
        : '';
    $('workflow-label').textContent = label + speculative + budget;
    bar.setAttribute('aria-label', 'Engineering workflow: ' + label);
    bar.classList.toggle('paused', stopped || workflow.state === 'requirements_review' || !!budget);
    bar.classList.toggle('rework', rework);
    bar.title =
      'Workflow stages, not an estimate of elapsed time or percentage complete. Revisions and rework can move backwards.';
  }
  async function refreshChat() {
    if (!session()) return;
    const id = session(),
      project = state.project;
    const flight = project + ':' + id;
    state.chatPending ||= new Set();
    if (state.chatPending.has(flight)) return;
    state.chatPending.add(flight);
    try {
      const [messages, statuses] = await Promise.all([
        json('/session/' + id + '/message' + scope()),
        json('/session/status' + scope()),
      ]);
      if (id !== session() || project !== state.project) return;
      const status = statuses?.[id]?.type || 'idle';
      const key =
        status +
        JSON.stringify(messages) +
        JSON.stringify(state.data?.artifacts.map(a => a.id) || []);
      if (key !== state.messagesKey) {
        state.messagesKey = key;
        renderMessages(messages, status);
        const previous = [...messages]
          .reverse()
          .find(
            m =>
              m.info.role === 'user' &&
              !m.parts?.some(
                p => p.type === 'text' && p.text?.startsWith('Attached workspace note ')
              )
          )?.info;
        if (previous?.model && !$('model').value) {
          $('model').value = previous.model.providerID + '::' + previous.model.modelID;
        }
      }
      $('chat-status').textContent = status;
      $('stop').disabled = status === 'idle' && !state.data?.harness?.enabled;
      $('send').disabled = status !== 'idle';
      await refreshRequests();
    } catch (e) {
      say(e.message);
    } finally {
      state.chatPending.delete(flight);
    }
  }
  async function selectSession(id) {
    if (!state.threads?.threads.some(t => t.kind === 'opencode' && t.id === id))
      throw Error('Conversation is outside this workspace');
    if (state.sessionId)
      state.chatDrafts[state.project + ':' + state.sessionId] = $('prompt').value;
    state.sessionId = id;
    wb.registry.get('ask').scope = 'Session / ' + id;
    wb.render();
    state.messagesKey = '';
    $('prompt').value = state.chatDrafts[state.project + ':' + id] || '';
    $('chat-title').textContent =
      state.threads.threads.find(t => t.id === id)?.title || 'Conversation';
    history.replaceState(null, '', chatPath(id));
    renderSidebar();
    await refreshChat();
  }
  async function newThread() {
    try {
      const value = await json('/session' + scope(), { title: 'Design conversation' });
      await refreshThreads();
      if (!state.main[state.project]) await setMain(value.id);
      await selectSession(value.id);
    } catch (e) {
      say(e.message);
    }
  }
  let requestKey = '';
  async function refreshRequests() {
    const id = session(),
      project = state.project;
    const [questions, permissions] = await Promise.all([
      json('/question' + scope()),
      json('/permission' + scope()),
    ]);
    if (id !== session() || project !== state.project) return;
    const current = {
      questions: (questions || []).filter(q => q.sessionID === id),
      permissions: (permissions || []).filter(p => p.sessionID === id),
    };
    const key = JSON.stringify(current);
    if (key === requestKey) return;
    requestKey = key;
    const area = $('requests');
    area.replaceChildren();
    for (const permission of current.permissions) {
      const card = node('section', undefined, 'request');
      card.append(
        node('strong', `Permission: ${permission.permission}`),
        node('pre', JSON.stringify(permission.patterns))
      );
      for (const [label, reply] of [
        ['Allow once', 'once'],
        ['Always allow', 'always'],
        ['Reject', 'reject'],
      ])
        card.append(
          button(label, async () => {
            try {
              await json('/permission/' + permission.id + '/reply' + scope(), { reply });
              requestKey = '';
              await refreshChat();
            } catch (e) {
              say(e.message);
            }
          })
        );
      area.append(card);
    }
    for (const question of current.questions) {
      const card = node('section', undefined, 'request');
      const answers = [];
      for (const [i, q] of question.questions.entries()) {
        card.append(node('h3', q.header || 'Review'), node('p', q.question));
        answers[i] = [];
        const options = node('div', undefined, 'question-options');
        for (const option of q.options || []) {
          const row = node('label', undefined, 'question-option');
          const input = node('input');
          input.type = q.multiple ? 'checkbox' : 'radio';
          input.name = question.id + '-' + i;
          input.value = option.label;
          input.onchange = () => {
            answers[i] = [...options.querySelectorAll('input:checked')].map(n => n.value);
          };
          row.append(input, node('strong', option.label));
          const description = node('div');
          richText(option.description || '', description);
          row.append(description);
          options.append(row);
        }
        card.append(options);
        if (q.custom !== false) {
          const custom = node('input');
          custom.placeholder = 'Your answer or refinement';
          custom.setAttribute('aria-label', q.question + ' — custom answer');
          custom.oninput = () => {
            answers[i] = custom.value.trim()
              ? [custom.value]
              : [...options.querySelectorAll('input:checked')].map(n => n.value);
          };
          card.append(custom);
        }
      }
      card.append(
        button('Submit answers', async () => {
          if (answers.some(a => !a.length)) {
            say('Answer each question before submitting.');
            return;
          }
          try {
            await json('/question/' + question.id + '/reply' + scope(), { answers });
            requestKey = '';
            await refreshChat();
          } catch (e) {
            say(e.message);
          }
        }),
        button('Dismiss', async () => {
          try {
            await json('/question/' + question.id + '/reject' + scope(), {});
            requestKey = '';
            await refreshChat();
          } catch (e) {
            say(e.message);
          }
        })
      );
      area.append(card);
    }
  }
  async function providers() {
    state.providers = await json('/yapnr/api/providers');
    const model = $('model'),
      selected = model.value;
    model.replaceChildren();
    model.append(node('option', 'Select a connected model'));
    model.firstChild.value = '';
    for (const provider of state.providers.all || []) {
      if (!state.providers.connected.includes(provider.id)) continue;
      const group = node('optgroup');
      group.label = provider.name || provider.id;
      for (const [id, m] of Object.entries(provider.models || {})) {
        const option = node('option', m.name || id);
        option.value = provider.id + '::' + id;
        group.append(option);
      }
      model.append(group);
    }
    model.value = selected;
  }
  $('send').onclick = async () => {
    const submittedContext = structuredClone({
      selection: state.selection,
      scope: state.selectionScope || null,
    });
    const text = $('prompt').value.trim();
    if (!text) return;
    if (!session()) {
      say('Create or select a conversation first.');
      return;
    }
    if (!$('model').value) {
      say('Select a connected model before sending.');
      return;
    }
    const [providerID, modelID] = $('model').value.split('::');
    $('send').disabled = true;
    try {
      await json(endpoint('message'), {
        session: session(),
        model: { providerID, modelID },
        text:
          text +
          (submittedContext.selection
            ? '\n\nSubmitted engineering context (frozen artifact scope):\n' +
              JSON.stringify(submittedContext, null, 2)
            : ''),
      });
      $('prompt').value = '';
      state.chatDrafts[state.project + ':' + session()] = '';
      localStorage.setItem('yapnr.ask-drafts', JSON.stringify(state.chatDrafts));
      await refreshChat();
    } catch (e) {
      say(e.message);
      $('send').disabled = false;
    }
  };
  $('prompt').onkeydown = e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (!$('send').disabled) $('send').click();
    }
  };
  $('resume-harness').onclick = async () => {
    try {
      await json(endpoint('harness-resume'), { session: session() });
      await refresh();
    } catch (e) {
      say(e.message);
    }
  };
  $('stop').onclick = async () => {
    try {
      await json(endpoint('harness-stop'), { session: session() });
      await refreshChat();
    } catch (e) {
      say(e.message);
    }
  };
  $('new-thread').onclick = newThread;
  $('expand-chat').onclick = () =>
    wb.dispatch({
      type: 'preset',
      name: wb.layout.preset === 'conversation' ? 'inspect' : 'conversation',
    });
  $('tabs').onclick = e => {
    const tab = e.target.dataset.tab;
    if (!tab) return;
    state.active = null;
    state.thread = null;
    if (tab === 'experiment') switchView('experiment');
    else openLegacy(tab, true);
    if (tab === 'threads') refreshThreads();
  };
  addEventListener('beforeunload', e => {
    if (state.dirty || Object.values(state.sourceViews).some(v => v.dirty())) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
  let eventStream = null,
    eventTimer = null;
  addEventListener('pagehide', () => {
    eventStream?.close();
    clearTimeout(eventTimer);
  });
  function subscribe() {
    eventStream?.close();
    clearTimeout(eventTimer);
    eventStream = new EventSource('/event' + scope());
    eventStream.onmessage = e => {
      try {
        const v = JSON.parse(e.data);
        if (['server.heartbeat', 'server.connected'].includes(v.type)) return;
        if (!eventTimer)
          eventTimer = setTimeout(() => {
            eventTimer = null;
            refreshChat();
            refresh();
          }, 120);
      } catch {}
    };
  }
  async function changeProject(name, wanted) {
    if (
      (state.dirty || Object.values(state.sourceViews).some(v => v.dirty())) &&
      !confirm('Discard unsaved document/source edits?')
    ) {
      $('project').value = state.project;
      return;
    }
    if (session()) state.chatDrafts[state.project + ':' + session()] = $('prompt').value;
    $('prompt').value = '';
    state.project = name;
    state.selection = null;
    $('selection-context').textContent = '';
    state.generation++;
    state.data = null;
    state.active = null;
    state.thread = null;
    state.threadDraft = null;
    state.threads = null;
    state.dirty = false;
    state.newNote = { title: '', body: '' };
    state.sessionId = '';
    state.messagesKey = '';
    requestKey = '';
    $('messages').replaceChildren();
    $('requests').replaceChildren();
    $('body').replaceChildren();
    state.disposeDesign?.();
    state.designKey = null;
    for (const [id, view] of Object.entries(state.sourceViews)) {
      view.discard();
      view.dispose();
      wb.registry.get(id).project = null;
    }
    state.sourceViews = {};
    for (const id of ['source', 'inspect']) wb.registry.get(id).element.replaceChildren();
    $('project').value = name;
    subscribe();
    wb.setProject(name);
    $('shell').hidden = false;
    $('close-project').textContent = 'Close project';
    for (const entry of Object.values(state.nativeViews)) delete entry.element.dataset.rendered;
    state.disposeTiming?.();
    state.timingProject = null;
    state.sourceViews.source?.dispose();
    wb.registry.get('source').project = null;
    startPresence(name).catch(e => say(e.message));
    await Promise.all([refresh(), refreshThreads()]);
    const chats = state.threads?.threads.filter(t => t.kind === 'opencode') || [];
    if (!state.main[name] && chats.length === 1) await setMain(chats[0].id);
    state.tab =
      state.data?.workflow.state === 'requirements_review'
        ? 'requirements'
        : state.data?.workflow.state === 'schematic' || state.data?.experiment_url
          ? 'experiment'
          : 'artifacts';
    const id = chats.find(t => t.id === wanted)?.id || state.main[name] || chats[0]?.id;
    if (id) await selectSession(id);
    else {
      $('chat-title').textContent = 'New conversation';
      history.replaceState(null, '', '/workspace/' + name);
      $('messages').append(node('p', 'Start a conversation to begin this project.', 'empty'));
    }
    if (state.data?.workflow.state === 'requirements_review') openLegacy('requirements');
    else activateView(wb.layout.panes[leavesForLayout()].active);
    function leavesForLayout() {
      let node = wb.layout.tree;
      while (!node.pane) node = node.first;
      return node.pane;
    }
  }
  $('project').onchange = () => changeProject($('project').value).catch(e => say(e.message));
  $('new-project').onclick = () => {
    $('project-dialog').showModal();
  };
  $('create-project').onclick = async () => {
    try {
      const name = $('project-name').value.trim();
      const result = await json('/yapnr/api/create-project', {
        name,
        directive: $('directive').value,
      });
      const option = node('option', name);
      option.value = name;
      $('project').append(option);
      $('project-dialog').close();
      await changeProject(result.project);
      await newThread();
      say('Project initialized. Send your directive to begin requirements and risk analysis.');
      $('prompt').value = $('directive').value;
    } catch (e) {
      $('project-error').textContent = e.message;
    }
  };
  $('connect').onclick = async () => {
    try {
      await providers();
      const methods = await json('/provider/auth');
      const select = $('provider');
      select.replaceChildren();
      for (const p of state.providers.all || []) {
        const o = node('option', p.name || p.id);
        o.value = p.id;
        select.append(o);
      }
      select.onchange = () => {
        const choices = $('auth-method');
        choices.replaceChildren();
        for (const [i, m] of (
          methods[select.value] || [{ type: 'api', label: 'API key' }]
        ).entries()) {
          const o = node('option', m.label || m.type);
          o.value = i;
          o.dataset.type = m.type;
          choices.append(o);
        }
      };
      select.onchange();
      $('auth-result').replaceChildren();
      $('provider-dialog').showModal();
    } catch (e) {
      say(e.message);
    }
  };
  $('authorize').onclick = async () => {
    try {
      const id = $('provider').value,
        method = Number($('auth-method').value),
        type = $('auth-method').selectedOptions[0]?.dataset.type;
      if (type === 'api') {
        const key = $('api-key').value.trim();
        if (!key) throw Error('Enter an API key.');
        await json('/auth/' + encodeURIComponent(id), { type: 'api', key }, 'PUT');
        $('api-key').value = '';
        await providers();
        $('provider-dialog').close();
        return;
      }
      const auth = await json('/provider/' + encodeURIComponent(id) + '/oauth/authorize', {
        method,
      });
      const result = $('auth-result');
      result.replaceChildren(
        node('p', auth.instructions || 'Complete authorization in the provider window.')
      );
      if (auth.url) {
        const a = node('a', 'Open provider authorization');
        a.href = auth.url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        result.append(a);
      }
      const code = node('input');
      code.placeholder = 'Authorization code (if requested)';
      result.append(
        code,
        button('Complete connection', async () => {
          try {
            await json('/provider/' + encodeURIComponent(id) + '/oauth/callback', {
              method,
              ...(code.value ? { code: code.value } : {}),
            });
            await providers();
            $('provider-dialog').close();
          } catch (e) {
            result.append(node('p', e.message, 'error'));
          }
        })
      );
    } catch (e) {
      $('auth-result').append(node('p', e.message, 'error'));
    }
  };
  $('connect-compatible').onclick = async () => {
    try {
      const id = $('compat-name').value.trim(),
        baseURL = $('compat-url').value.trim(),
        model = $('compat-model').value.trim();
      if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(id) || !model)
        throw Error('Provide a connection name and model ID.');
      const url = new URL(baseURL);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
        throw Error('Use an HTTP(S) API base URL without credentials in the URL.');
      const existing = await json('/config');
      const provider = {
        ...(existing.provider || {}),
        [id]: {
          npm: '@ai-sdk/openai-compatible',
          name: id,
          options: { baseURL, ...($('compat-key').value ? { apiKey: $('compat-key').value } : {}) },
          models: { [model]: { name: model } },
        },
      };
      await json('/config', { provider }, 'PATCH');
      $('compat-key').value = '';
      await providers();
      $('model').value = id + '::' + model;
      $('provider-dialog').close();
      say('Compatible endpoint saved in the agent runtime configuration.');
    } catch (e) {
      $('auth-result').append(node('p', e.message, 'error'));
    }
  };
  addEventListener('message', event => {
    const frame = Object.values(state.viewerFrames).find(
      f => f && event.source === f.contentWindow
    );
    if (!frame || event.origin !== new URL(frame.src).origin) return;
    const data = event.data;
    if (data?.type === 'yapnr-experiment-selection' && frame === state.viewerFrames.experiments) {
      for (const id of ['board', 'schematic', 'three'])
        sendView(state.viewerFrames[id], 'yapnr-select-lane', { lane: data.lane });
      return;
    }
    if (data?.type === 'yapnr-view-error') return say('Viewer: ' + data.error);
    if (data?.type === 'yapnr-comparison-ready') {
      const id = 'comparison:' + state.project + ':' + data.pin,
        base = new URL(state.data.experiment_url);
      base.searchParams.set('checkpoint', data.pin);
      base.searchParams.set('lane', data.scope.lane);
      base.searchParams.set('phase', data.scope.phase);
      const entry = wb.register({
        id,
        title: 'Pinned Board · ' + data.pin.slice(0, 8),
        type: 'board',
        project: state.project,
        viewerUrl: base.href,
        scope:
          'Pinned artifact / ' + (data.scope.board_sha256 || data.scope.layout_sha256 || data.pin),
      });
      state.viewerFrames[id] = mountViewer(entry.element, base.href, 'board');
      wb.open(id);
      return;
    }
    if (data?.type === 'yapnr-central-interaction') {
      wb.dismiss();
      return;
    }
    if (data?.type === 'yapnr-view-ready') {
      const entry = wb.registry.get(
        Object.keys(state.viewerFrames).find(id => state.viewerFrames[id] === frame)
      );
      if (!entry) return;
      entry.scope =
        (entry.viewerUrl?.includes('checkpoint=')
          ? 'Pinned artifact / '
          : entry.type === 'source'
            ? 'Reference source / '
            : 'Artifact / ') +
        (data.scope?.board_sha256?.slice(0, 12) || 'hash unavailable') +
        ' / ' +
        (data.scope?.lane || 'no lane') +
        ' / ' +
        (data.scope?.phase || 'live');
      frame.dataset.scope = JSON.stringify(data.scope || {});
      wb.render();
      syncTheme();
      return;
    }
    if (data?.type === 'yapnr-context-mismatch')
      return say('Selection revision differs from this view. Highlight not applied.');
    if (data?.type === 'yapnr-controls-state' && frame === state.controlsTarget) {
      const body = wb.registry.get('controls').element;
      body.replaceChildren(node('h3', 'View controls'));
      const targets = node('select');
      targets.setAttribute('aria-label', 'View controls target');
      const follow = node('option', 'Follow active engineering view');
      follow.value = '';
      targets.append(follow);
      for (const [id, f] of Object.entries(state.viewerFrames)) {
        if (!f || !['board', 'schematic', 'three'].includes(id)) continue;
        const o = node('option', 'Pin to ' + id);
        o.value = id;
        targets.append(o);
      }
      targets.value = state.pinnedControls || '';
      targets.onchange = () => {
        state.pinnedControls = targets.value;
        requestControls();
      };
      body.append(
        targets,
        node('p', 'Target: ' + frame.dataset.mode),
        button('Pin comparison view', () => sendView(frame, 'yapnr-pin-comparison'))
      );
      for (const item of data.items || []) {
        let input;
        if (item.type === 'button')
          input = button(item.label, () => sendView(frame, 'yapnr-view-command', { id: item.id }));
        else if (item.options) {
          input = node('select');
          for (const option of item.options) {
            const o = node('option', option.label);
            o.value = option.value;
            input.append(o);
          }
          input.value = item.value;
        } else {
          input = node('input');
          input.type = item.type === 'checkbox' ? 'checkbox' : 'text';
          input.checked = !!item.checked;
          input.value = item.value || '';
        }
        input.onchange = () =>
          sendView(frame, 'yapnr-view-command', {
            id: item.id,
            value: input.value,
            checked: input.checked,
          });
        const label = node('label', item.type === 'button' ? '' : item.label);
        label.append(input);
        body.append(label);
      }
      return;
    }
    if (!['yapnr-selection', 'yapnr-ask-selection'].includes(data?.type)) return;
    state.selection = structuredClone(data.selection);
    state.selectionScope = data.scope || JSON.parse(frame.dataset.scope || '{}');
    $('selection-context').textContent = state.selection
      ? 'Context: ' + (state.selection.ref || state.selection.name || state.selection.kind)
      : '';
    const inspect = wb.registry.get('inspect').element;
    inspect.replaceChildren(
      node('h3', state.selection?.ref || state.selection?.name || 'Selection'),
      node(
        'pre',
        JSON.stringify({ selection: state.selection, scope: state.selectionScope }, null, 2)
      ),
      node(
        'p',
        'Source-index metadata is shown when available; equivalence to the built artifact is not recorded.'
      ),
      button('Reveal', () =>
        sendView(frame, 'yapnr-linked-selection', {
          selection: state.selection,
          scope: state.selectionScope,
          reveal: true,
        })
      ),
      button('Add to Ask', () => {
        wb.open('ask');
        $('prompt').focus();
      })
    );
    for (const [key, label] of [
      ['octopart_url', 'Octopart'],
      ['easyeda_url', 'EasyEDA'],
      ['datasheet_url', 'Datasheet'],
    ]) {
      const link = data.record?.part_links?.[key] || data.record?.[key];
      try {
        const url = new URL(link);
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) continue;
        const a = node('a', label);
        a.href = url.href;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        inspect.append(a, node('br'));
      } catch {}
    }
    if (data.record) {
      const part = data.record;
      for (const key of ['type', 'part', 'instance', 'value', 'footprint', 'doc'])
        if (typeof part[key] === 'string') inspect.append(node('p', part[key]));
      if (part.file)
        inspect.append(
          button('Source · ' + part.file + ':' + (part.line || 1), () => openSource(part.file))
        );
      if (part.pins) {
        const table = node('table');
        const header = node('tr');
        for (const title of ['Pad', 'Pin', 'Net']) header.append(node('th', title));
        table.append(header);
        for (const [pad, pin] of Object.entries(part.pins)) {
          const row = node('tr');
          for (const text of [pad, pin.pin || '', pin.net || 'unconnected'])
            row.append(node('td', text));
          table.append(row);
        }
        inspect.append(table);
      }
      const details = node('details');
      details.append(
        node('summary', 'Source-index record'),
        node('pre', JSON.stringify(part, null, 2))
      );
      inspect.append(details);
    }
    for (const other of Object.values(state.viewerFrames))
      if (other && other !== frame)
        sendView(other, 'yapnr-linked-selection', {
          selection: state.selection,
          scope: state.selectionScope,
        });
    if (data.type === 'yapnr-ask-selection') {
      wb.open('ask');
      $('prompt').focus();
    }
  });
  $('clear-context').onclick = () => {
    state.selection = null;
    $('selection-context').textContent = '';
  };
  // Pointer resizing keeps chat and engineering views in one layout.
  $('resize-chat').onpointerdown = e => {
    const handle = e.currentTarget;
    handle.setPointerCapture(e.pointerId);
    handle.onpointermove = move => {
      document.documentElement.style.setProperty(
        '--chat-width',
        Math.min(innerWidth * 0.75, Math.max(320, innerWidth - move.clientX)) + 'px'
      );
    };
    handle.onpointerup = handle.onpointercancel = () => (handle.onpointermove = null);
  };
  async function boot() {
    const listing = await json('/yapnr/api/projects');
    for (const name of listing.projects) {
      const o = node('option', name);
      o.value = name;
      $('project').append(o);
    }
    let wanted = location.pathname.match(/\/session\/(ses_[A-Za-z0-9]+)/)?.[1];
    let name = location.pathname.match(/^\/workspace\/([a-z0-9-]+)/)?.[1];
    if (wanted && !name) {
      try {
        name = (await json('/yapnr/api/session-project/' + wanted)).project;
      } catch {}
    }
    if (!name) {
      try {
        const decoded = atob(location.pathname.split('/')[1].replace(/-/g, '+').replace(/_/g, '/'));
        name = decoded.match(/^\/projects\/([a-z0-9-]+)$/)?.[1];
      } catch {}
    }
    if (name && !listing.projects.includes(name)) name = null;
    const providerLoad = providers().catch(e => say(e.message));
    if (name || listing.projects[0]) await changeProject(name || listing.projects[0], wanted);
    else {
      $('body').append(node('p', 'Create your first project to begin.', 'empty'));
    }
    $('loading').remove();
    performance.mark('yapnr:workspace-ready');
    await providerLoad;
    performance.mark('yapnr:providers-ready');
  }
  boot().catch(e => {
    say(e.message);
    $('loading').textContent = 'Unable to connect: ' + e.message;
  });
  setInterval(() => {
    refresh();
    refreshChat();
  }, 2500);
  setInterval(refreshThreads, 10000);
})();
