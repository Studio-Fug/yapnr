/* Workspace-native UI. OpenCode supplies the runtime API, never the page shell. */
import { mountTraceability } from '/yapnr/traceability.js';

(() => {
  const shadow = document;
  const $ = id => document.getElementById(id);
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
    chatDrafts: {},
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
  function openPanel() {
    document.body.classList.remove('chat-expanded');
  }
  function switchView(mode) {
    state.mode = mode;
    if (mode === 'chat') {
      document.body.classList.add('chat-expanded');
      return;
    }
    openPanel();
    state.tab = 'experiment';
    $('view-title').textContent = 'Experiment';
    $('body').replaceChildren();
    $('body').hidden = true;
    $('experiment').hidden = false;
    const url = state.data?.experiment_url;
    if (!url) {
      $('experiment').replaceChildren();
      return;
    }
    const embedded = new URL(url, location.href);
    embedded.searchParams.set('workspace', '1');
    let frame = $('experiment').querySelector('iframe');
    if (!frame || frame.src !== embedded.href) {
      frame = node('iframe');
      frame.src = embedded.href;
      frame.title = `${state.project} experiment`;
      frame.addEventListener('load', () => (frame.dataset.loaded = '1'));
      $('experiment').replaceChildren(frame);
    }
  }
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
        t.kind === 'opencode'
          ? selectSession(t.id)
          : ((state.tab = 'threads'), openPanel(), showThread(t))
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
      const reviewKey = project + ':' + data.workflow.revision;
      if (data.workflow.state === 'requirements_review' && state.reviewNotification !== reviewKey) {
        state.reviewNotification = reviewKey;
        state.tab = 'requirements';
        state.active = null;
        render();
      }

      const captureKey = project + ':' + data.workflow.revision;
      if (data.workflow.state === 'schematic' && state.captureNotification !== captureKey) {
        state.captureNotification = captureKey;
        state.active = null;
        switchView('experiment');
      }

      $('recording').textContent = `Recording: ${data.recording}`;
      renderSidebar();
      if (!state.dirty && state.tab === 'scratchpad' && !state.active) {
        const edit = $('scratch');
        if (edit && document.activeElement !== edit) {
          edit.value = data.scratchpad.text;
          state.revision = data.scratchpad.revision;
        }
      }
      if (state.tab === 'experiment') switchView('experiment');
      else if (!$('body').childElementCount) render();
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
            switchView('experiment');
            const frame = $('experiment').querySelector('iframe');
            const send = () =>
              frame.contentWindow.postMessage(
                { type: 'yapnr-open-thread', session: t.id },
                new URL(state.data.experiment_url, location.href).origin
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
        render();
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
                render();
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
          render();
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
    $('experiment').hidden = true;
    if (state.tab === 'requirements') {
      const article = state.project;
      state.active = null;
      body.append(node('p', 'Loading requirements model…'));
      json(endpoint('requirements'))
        .then(data => {
          if (state.tab === 'requirements' && state.project === article) {
            mountTraceability(body, data, {
              openArtifact: showArtifact,
              ask: text => {
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
  async function showArtifact(item) {
    $('experiment').hidden = true;
    $('body').hidden = false;
    $('view-title').textContent = item.title;
    state.active = item;
    state.strokes = [];
    state.view = null;
    const body = $('body');
    body.replaceChildren(
      button('← Artifacts', () => {
        state.active = null;
        render();
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
  function imageEditor(view, actions, url, item) {
    const image = new Image();
    image.onload = () => {
      if (state.active?.id !== item.id) return;
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
        for (const points of state.strokes) {
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
        state.strokes.push(drawing);
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
          state.strokes.pop();
          redraw();
        }),
        button('Save annotation', async () => {
          try {
            const annotated = await json(endpoint('annotation'), {
              artifact: item.id,
              image: canvas.toDataURL('image/png'),
              strokes: state.strokes,
              view: state.view,
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
              strokes: state.strokes,
              view: state.view,
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
    if (
      !['yapnr-ready', 'yapnr-snapshot'].includes(value?.type) ||
      !state.active ||
      value.artifact !== state.active.id
    )
      return;
    const frame = shadow.querySelector('iframe[data-yapnr-artifact]');
    if (!frame || event.source !== frame.contentWindow) return;
    if (value.type === 'yapnr-ready') {
      const capture = shadow.querySelector('button[data-scene-capture]');
      if (capture) capture.disabled = false;
      return;
    }
    if (!value.image?.startsWith('data:image/png;base64,')) {
      say('Scene has no valid render yet. Try again after it loads.');
      return;
    }
    state.view = value.view;
    const view = $('body').querySelector('.view'),
      actions = $('body').querySelector('.actions');
    imageEditor(view, actions, value.image, state.active);
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
        for (const part of tools) {
          const d = node('details', undefined, 'tool');
          d.dataset.part = part.id;
          d.open = expanded.has(part.id);
          d.append(
            node('summary', `${part.tool} · ${part.state?.status || ''}`),
            node('pre', JSON.stringify(part.state, null, 2))
          );
          rollup.append(d);
        }
        for (const part of reasoning) {
          const d = node('details');
          d.dataset.part = part.id;
          d.open = expanded.has(part.id);
          d.append(node('summary', 'Reasoning'), node('div', part.text, 'message-text'));
          rollup.append(d);
        }
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
    }
  }
  async function selectSession(id) {
    if (!state.threads?.threads.some(t => t.kind === 'opencode' && t.id === id))
      throw Error('Conversation is outside this workspace');
    if (state.sessionId)
      state.chatDrafts[state.project + ':' + state.sessionId] = $('prompt').value;
    state.sessionId = id;
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
    state.providers = await json('/provider');
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
          (state.selection
            ? '\n\nSelected engineering context (inspect against current artifact):\n' +
              JSON.stringify(state.selection, null, 2)
            : ''),
      });
      $('prompt').value = '';
      state.chatDrafts[state.project + ':' + session()] = '';
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
  $('expand-chat').onclick = () => document.body.classList.toggle('chat-expanded');
  $('tabs').onclick = e => {
    const tab = e.target.dataset.tab;
    if (!tab) return;
    state.tab = tab;
    state.active = null;
    state.thread = null;
    openPanel();
    render();
    if (tab === 'threads') refreshThreads();
  };
  let eventStream = null,
    eventTimer = null;
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
    if (state.dirty && !confirm('Discard unsaved document edits?')) {
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
    $('experiment').replaceChildren();
    $('experiment').hidden = true;
    $('project').value = name;
    subscribe();
    await refresh();
    await refreshThreads();
    const chats = state.threads?.threads.filter(t => t.kind === 'opencode') || [];
    if (!state.main[name] && chats.length === 1) await setMain(chats[0].id);
    state.tab = state.data?.workflow.state === 'requirements_review'
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
    render();
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
    const frame = $('experiment').querySelector('iframe');
    if (
      !frame ||
      event.source !== frame.contentWindow ||
      event.origin !== new URL(frame.src).origin ||
      !['yapnr-selection', 'yapnr-ask-selection'].includes(event.data?.type)
    )
      return;
    state.selection = event.data.selection;
    $('selection-context').textContent = state.selection
      ? `Context: ${
          state.selection.ref || state.selection.name || state.selection.kind || 'selection'
        }`
      : '';
    if (event.data.type === 'yapnr-ask-selection') $('prompt').focus();
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
    await providers();
    if (name || listing.projects[0]) await changeProject(name || listing.projects[0], wanted);
    else {
      $('body').append(node('p', 'Create your first project to begin.', 'empty'));
    }
    $('loading').remove();
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
