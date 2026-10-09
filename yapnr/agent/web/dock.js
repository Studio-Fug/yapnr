import { initialLayout, transact, leaves, location, validate } from './dock-model.js';
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
const button = (text, label, fn) => {
  const b = el('button', null, text);
  b.type = 'button';
  b.setAttribute('aria-label', label || text);
  b.onclick = fn;
  return b;
};
export class Workbench {
  constructor(root, { activate, announce, close, layout } = {}) {
    this.root = root;
    this.onActivate = activate;
    this.announce = announce || (() => {});
    this.onClose = close;
    this.onLayout = layout;
    this.registry = new Map();
    this.panes = new Map();
    this.undo = [];
    this.project = '';
    this.layout = initialLayout();
    this.viewLayer = el('div', 'view-layer');
    this.parking = el('div', 'view-parking');
    this.parking.hidden = true;
    this.center = el('div', 'dock-center');
    this.center.id = 'dock-center';
    this.center.setAttribute('aria-label', 'Engineering views');
    this.center.addEventListener('pointerdown', () => this.dismiss());
    this.center.addEventListener('wheel', () => this.dismiss(), { passive: true });
    this.center.addEventListener('focusin', e => {
      if (e.isTrusted) this.dismiss();
    });
    this.left = this.drawer('left');
    this.right = this.drawer('right');
    this.shelf = el('div', 'pane-shelf');
    this.shelf.setAttribute('aria-label', 'Minimized panes');
    this.mobile = el('select', 'pane-switcher');
    this.mobile.setAttribute('aria-label', 'Visible pane');
    this.mobile.onchange = () => this.dispatch({ type: 'mobile', pane: this.mobile.value });
    root.append(
      this.left.rail,
      this.left.node,
      this.mobile,
      this.center,
      this.right.node,
      this.right.rail,
      this.shelf,
      this.parking,
      this.viewLayer
    );
    this.capture = el('div', 'drag-capture');
    this.capture.hidden = true;
    document.body.append(this.capture);
    this.preview = el('div', 'drop-preview');
    this.preview.hidden = true;
    this.ghost = el('div', 'drag-object yp-glass');
    this.ghost.hidden = true;
    document.body.append(this.preview, this.ghost);
    addEventListener('resize', () => this.project && this.render());
    addEventListener('blur', () => this.cancelDrag());
    addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        if (this.cancelResize) {
          e.preventDefault();
          this.cancelResize();
          return;
        }
        if (this.drag) {
          e.preventDefault();
          this.cancelDrag();
        } else {
          const active = document.activeElement?.closest('[data-view-id]'),
            side = ['left', 'right'].find(
              s =>
                (this[s].node.contains(document.activeElement) ||
                  location(this.layout, active?.dataset.viewId) === s) &&
                this.layout.drawers[s].open
            );
          if (side) this.dispatch({ type: 'drawer', side, values: { open: false } });
        }
      }
    });
  }
  drawer(side) {
    const rail = el('div', 'drawer-rail'),
      node = el('aside', 'tool-drawer yp-glass');
    node.dataset.side = side;
    const pull = button(side === 'left' ? 'Experiments' : 'Ask', `Open ${side} drawer`, () => {
      this.dispatch({ type: 'drawer', side, values: { open: !this.layout.drawers[side].open } });
      if (this.layout.drawers[side].open) {
        this.onActivate?.(this.layout.panes[side].active);
        this.panes.get(side)?.querySelector('[aria-selected="true"]')?.focus();
      }
    });
    pull.className = 'drawer-pull';
    const lock = button('◇', `Lock ${side} drawer open`, () =>
      this.dispatch({
        type: 'drawer',
        side,
        values: { locked: !this.layout.drawers[side].locked, open: true },
      })
    );
    rail.append(lock, pull);
    rail.addEventListener('pointerenter', () => {
      if (!this.drag) return;
      this.hoverTimer = setTimeout(() => {
        if (this.drag) {
          this.layout.drawers[side].open = true;
          this.render();
        }
      }, 250);
    });
    rail.addEventListener('pointerleave', () => clearTimeout(this.hoverTimer));
    const resize = el('div', 'drawer-resizer');
    resize.tabIndex = 0;
    resize.setAttribute('role', 'separator');
    resize.setAttribute('aria-label', 'Resize ' + side + ' drawer');
    resize.setAttribute('aria-orientation', 'vertical');
    const width = value =>
      this.dispatch({
        type: 'drawer',
        side,
        values: { width: Math.max(280, Math.min(innerWidth - 72, value)) },
      });
    resize.onkeydown = e => {
      if (['ArrowLeft', 'ArrowRight'].includes(e.key)) {
        e.preventDefault();
        width(
          this.layout.drawers[side].width +
            (e.key === 'ArrowRight' ? 1 : -1) * (side === 'left' ? 1 : -1) * 24
        );
        this.announce('Drawer width ' + this.layout.drawers[side].width + ' pixels');
      }
    };
    resize.onpointerdown = e => {
      e.preventDefault();
      const before = structuredClone(this.layout),
        x = e.clientX,
        w = this.layout.drawers[side].width;
      resize.setPointerCapture(e.pointerId);
      const move = v => {
        this.layout.drawers[side].width = Math.max(
          280,
          Math.min(innerWidth - 72, w + (v.clientX - x) * (side === 'left' ? 1 : -1))
        );
        node.style.setProperty('--drawer-width', this.layout.drawers[side].width + 'px');
        this.updateViews();
      };
      const end = v => {
        this.cancelResize = null;
        resize.removeEventListener('pointermove', move);
        resize.removeEventListener('pointerup', end);
        resize.removeEventListener('pointercancel', end);
        if (v.type === 'pointercancel') this.layout = before;
        else this.undo.push(before);
        this.save();
        this.render();
      };
      this.cancelResize = () => end({ type: 'pointercancel' });
      resize.addEventListener('pointermove', move);
      resize.addEventListener('pointerup', end);
      resize.addEventListener('pointercancel', end);
    };
    node.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        e.preventDefault();
        this.dispatch({ type: 'drawer', side, values: { open: false } });
        pull.focus();
      }
    });
    return { rail, node, pull, lock, resize };
  }
  register(tab, element) {
    const existing = this.registry.get(tab.id);
    if (existing) {
      if (existing.type === 'missing' && tab.type !== 'missing') existing.element.replaceChildren();
      Object.assign(existing, tab);
      return existing;
    }
    const entry = {
      ...tab,
      element: element || el('div', 'tab-content'),
      scope: tab.scope || 'Working tree',
    };
    entry.element.classList.add('tab-content');
    entry.element.dataset.viewId = tab.id;
    entry.element.setAttribute('role', 'tabpanel');
    if (!entry.element.id) entry.element.id = 'view-' + encodeURIComponent(tab.id);
    if (tab.type === 'missing')
      entry.element.append(
        el('p', 'empty', 'This view is unavailable. Close it or open an available project view.')
      );
    this.registry.set(tab.id, entry);
    this.viewLayer.append(entry.element);
    for (const event of ['pointerdown', 'wheel', 'focusin'])
      entry.element.addEventListener(
        event,
        () => {
          const p = location(this.layout, tab.id);
          if (p && !['left', 'right'].includes(p)) {
            this.activeCentral = p;
            this.dismiss();
          }
        },
        { passive: true }
      );
    return entry;
  }
  setProject(project) {
    if (this.project) this.save();
    this.project = project;
    this.undo = [];
    try {
      this.layout =
        validate(JSON.parse(localStorage.getItem('yapnr.layout.v1.' + project))) || initialLayout();
    } catch {
      this.layout = initialLayout();
    }
    if (!location(this.layout, 'source-browser') && !this.layout.closed.includes('source-browser'))
      this.layout.panes.left.tabs.unshift('source-browser');
    for (const entry of Object.values(this.layout.bindings || {}))
      if (!this.registry.has(entry.id)) this.register({ ...entry, type: 'missing' });
    this.render();
  }
  save() {
    this.layout.bindings = Object.fromEntries(
      [...this.registry]
        .filter(([, v]) => v.project === this.project && (v.path || v.viewerUrl || v.artifact))
        .map(([id, v]) => [
          id,
          {
            id,
            title: v.title,
            type: v.type,
            project: v.project,
            path: v.path,
            viewerUrl: v.viewerUrl,
            artifact: v.artifact,
            scope: v.scope,
          },
        ])
    );
    if (this.project)
      localStorage.setItem('yapnr.layout.v1.' + this.project, JSON.stringify(this.layout));
  }
  dispatch(action, { undo = true, focus = false } = {}) {
    try {
      if (action.type === 'drawer' && action.values?.open) this.overlaySide = action.side;
      const next = transact(this.layout, action);
      const destination = action.target || location(next, action.tab);
      if (['left', 'right'].includes(destination) && next.drawers[destination].open)
        this.overlaySide = destination;
      if (undo) this.undo.push(structuredClone(this.layout));
      this.undo = this.undo.slice(-40);
      this.layout = next;
      this.save();
      this.render();
      if (focus) this.focus(action.tab || this.layout.panes[action.target]?.active);
      return true;
    } catch (e) {
      this.announce(e.message);
      return false;
    }
  }
  undoLayout() {
    const previous = this.undo.pop();
    if (!previous) return;
    this.layout = previous;
    this.save();
    this.render();
    this.announce('Layout restored. Project data unchanged.');
  }
  open(id, pane) {
    if (
      this.layout.preset === 'conversation' &&
      ['board', 'schematic', 'three', 'source'].includes(id)
    )
      this.dispatch({ type: 'preset', name: 'inspect' });
    this.dispatch({ type: 'open', tab: id, pane }, { focus: true });
  }
  focus(id) {
    this.lastFocusedTab = id;
    const p = location(this.layout, id);
    this.panes
      .get(p)
      ?.querySelector(`[data-tab-id="${CSS.escape(id)}"]`)
      ?.focus();
  }
  async closeTab(id) {
    if (this.onClose && !(await this.onClose([id]))) return;
    const origin = location(this.layout, id);
    this.dispatch({ type: 'close', tab: id });
    this.focus(
      this.layout.panes[origin]?.active || this.layout.panes[leaves(this.layout.tree)[0]].active
    );
    this.announce('View closed. Project data retained.');
  }
  async closePane(id) {
    const tabs = [...this.layout.panes[id].tabs];
    if (this.onClose && !(await this.onClose(tabs))) return;
    const previous = structuredClone(this.layout);
    for (const tab of tabs) this.layout = transact(this.layout, { type: 'close', tab });
    this.undo.push(previous);
    this.save();
    this.render();
  }
  dismiss() {
    if (this.drag || this.rendering) return;
    let changed = false;
    for (const side of ['left', 'right']) {
      const d = this.layout.drawers[side];
      if (d.open && !d.locked) {
        d.open = false;
        changed = true;
      }
    }
    if (changed) {
      this.save();
      this.render();
    }
  }
  pane(id) {
    let p = this.panes.get(id);
    if (p) return p;
    p = el('section', 'dock-pane');
    p.dataset.pane = id;
    const header = el('div', 'pane-header'),
      grip = button('⋮⋮', 'Move whole pane group', () => this.menu(id));
    grip.className = 'pane-grip';
    grip.onpointerdown = e => this.beginDrag(e, { pane: id });
    const strip = el('div', 'tab-strip');
    strip.setAttribute('role', 'tablist');
    strip.setAttribute('aria-label', id + ' views');
    const menu = button('⋯', 'Pane actions', () => this.menu(id));
    menu.className = 'pane-menu';
    header.append(grip, strip, menu);
    const scope = el('div', 'pane-scope'),
      body = el('div', 'pane-content');
    p.append(header, scope, body);
    p.addEventListener('pointerdown', () => {
      if (!['left', 'right'].includes(id)) this.activeCentral = id;
    });
    this.panes.set(id, p);
    return p;
  }
  renderPane(id) {
    const p = this.pane(id),
      model = this.layout.panes[id],
      strip = p.querySelector('.tab-strip'),
      body = p.querySelector('.pane-content'),
      scope = p.querySelector('.pane-scope');
    const focused = document.activeElement?.dataset.tabId;
    strip.replaceChildren();
    for (const tabId of model.tabs) {
      const t =
        this.registry.get(tabId) || this.register({ id: tabId, title: tabId, type: 'missing' });
      const tab = button(t.title || tabId, null, () => {
        this.dispatch({ type: 'activate', tab: tabId }, { undo: false });
        this.onActivate?.(tabId);
      });
      tab.dataset.tabId = tabId;
      tab.setAttribute('role', 'tab');
      tab.id = 'tab-' + encodeURIComponent(tabId);
      tab.setAttribute('aria-controls', t.element.id);
      t.element.setAttribute('aria-labelledby', tab.id);
      tab.setAttribute('aria-selected', String(model.active === tabId));
      tab.tabIndex = model.active === tabId ? 0 : -1;
      tab.title = (t.path || t.title) + ' · ' + t.scope;
      tab.onpointerdown = e => this.beginDrag(e, { tab: tabId });
      tab.onkeydown = e => {
        if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
          e.preventDefault();
          const index = model.tabs.indexOf(tabId),
            next =
              e.key === 'Home'
                ? 0
                : e.key === 'End'
                  ? model.tabs.length - 1
                  : (index + (e.key === 'ArrowRight' ? 1 : -1) + model.tabs.length) %
                    model.tabs.length;
          this.dispatch({ type: 'activate', tab: model.tabs[next] }, { undo: false, focus: true });
          this.onActivate?.(model.tabs[next]);
        }
        if (e.shiftKey && e.key === 'F10') {
          e.preventDefault();
          this.menu(id, tabId);
        }
      };
      const close = button('×', 'Close ' + t.title, () => this.closeTab(tabId));
      close.className = 'tab-close';
      const wrapper = el('div', 'tab-item');
      wrapper.dataset.tabId = tabId;
      wrapper.append(tab, close);
      strip.append(wrapper);
      t.element.hidden = model.active !== tabId;
      t.element.dataset.paneLocation = id;
    }

    if (!model.tabs.length) {
      if (!body.querySelector('.empty-pane')) {
        const empty = button('Open a view', 'Open a view', () => this.menu(id));
        empty.className = 'empty-pane';
        body.append(empty);
      }
    }
    if (model.tabs.length) body.replaceChildren();
    const active = this.registry.get(model.active);
    scope.textContent = active?.scope || 'Project views';
    if (focused && strip.querySelector(`[data-tab-id="${CSS.escape(focused)}"]`))
      strip.querySelector(`[data-tab-id="${CSS.escape(focused)}"]`).focus({ preventScroll: true });
    return p;
  }
  render() {
    const drawerBefore = Object.fromEntries(
      ['left', 'right'].map(side => {
        const ui = this[side];
        const visible = ui.visible ?? !ui.node.hidden;
        const rect = ui.node.getBoundingClientRect();
        return [side, { visible, rect }];
      })
    );
    this.rendering = true;
    const narrow = innerWidth < 900;
    const centralMin = this.minimum(this.layout.tree, 'x');
    let space = innerWidth - 72 - centralMin;
    const reserve = {};
    for (const side of ['right', 'left']) {
      const d = this.layout.drawers[side];
      reserve[side] = !narrow && d.open && d.locked && space >= d.width;
      if (reserve[side]) space -= d.width;
    }
    this.root.dataset.narrow = String(narrow);
    this.root.dataset.preset = this.layout.preset;
    this.center.replaceChildren();
    const build = (node, path = []) => {
      if (node.pane) return this.renderPane(node.pane);
      const split = el('div', 'dock-split ' + (node.axis === 'x' ? 'horizontal' : 'vertical'));
      split.style.setProperty('--ratio', node.ratio);
      const a = build(node.first, [...path, 'first']),
        b = build(node.second, [...path, 'second']);
      const handle = el('div', 'dock-splitter');
      handle.dataset.splitPath = path.join('.');
      handle.setAttribute('role', 'separator');
      handle.tabIndex = 0;
      handle.setAttribute('aria-orientation', node.axis === 'x' ? 'vertical' : 'horizontal');
      handle.setAttribute('aria-label', 'Resize panes');
      handle.setAttribute('aria-valuenow', String(Math.round(node.ratio * 100)));
      handle.onkeydown = e => {
        if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
          e.preventDefault();
          const r = split.getBoundingClientRect(),
            span = node.axis === 'x' ? r.width : r.height,
            min = this.minimum(node.first, node.axis) / span,
            max = 1 - this.minimum(node.second, node.axis) / span;
          if (min > max) return;
          const ratio = Math.max(
            min,
            Math.min(max, node.ratio + (['ArrowLeft', 'ArrowUp'].includes(e.key) ? -0.03 : 0.03))
          );
          this.dispatch({ type: 'resize', path, ratio });
          this.center
            .querySelector('[data-split-path="' + path.join('.') + '"]')
            ?.focus({ preventScroll: true });
          this.announce('Pane ratio ' + Math.round(ratio * 100) + ' percent');
        }
      };
      handle.onpointerdown = e => {
        e.preventDefault();
        const rect = split.getBoundingClientRect(),
          before = structuredClone(this.layout);
        handle.setPointerCapture(e.pointerId);
        const move = event => {
          const span = node.axis === 'x' ? rect.width : rect.height,
            min = this.minimum(node.first, node.axis) / span,
            max = 1 - this.minimum(node.second, node.axis) / span;
          if (min > max) return;
          const ratio =
            (node.axis === 'x' ? event.clientX - rect.left : event.clientY - rect.top) / span;
          node.ratio = Math.min(max, Math.max(min, ratio));
          this.updateViews();
          split.style.setProperty('--ratio', node.ratio);
          handle.setAttribute('aria-valuenow', String(Math.round(node.ratio * 100)));
        };
        const end = event => {
          this.cancelResize = null;
          handle.removeEventListener('pointermove', move);
          handle.removeEventListener('pointerup', end);
          handle.removeEventListener('pointercancel', cancel);
          if (event.type !== 'pointercancel') {
            this.undo.push(before);
            this.save();
          } else {
            this.layout = before;
            this.render();
          }
        };
        const cancel = event => end(event);
        this.cancelResize = () => end({ type: 'pointercancel' });
        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', end);
        handle.addEventListener('pointercancel', cancel);
      };
      split.append(a, handle, b);
      return split;
    };
    let tree = this.layout.tree;
    if (this.layout.maximized && leaves(tree).includes(this.layout.maximized))
      tree = { pane: this.layout.maximized };
    const projected =
      narrow ||
      this.root.clientHeight < this.minimum(tree, 'y') ||
      this.root.clientWidth -
        72 -
        Object.keys(reserve).reduce(
          (n, s) => n + (reserve[s] ? this.layout.drawers[s].width : 0),
          0
        ) <
        this.minimum(tree, 'x');
    this.root.dataset.narrow = String(projected);
    if (projected)
      tree = {
        pane: leaves(this.layout.tree).includes(this.layout.mobilePane)
          ? this.layout.mobilePane
          : leaves(this.layout.tree)[0],
      };
    this.center.append(build(tree));
    for (const [id, p] of this.panes)
      if (!['left', 'right'].includes(id) && !leaves(tree).includes(id)) this.parking.append(p);
    for (const side of ['left', 'right']) {
      const d = this.layout.drawers[side],
        ui = this[side];
      if (ui.motion && d.open !== ui.visible) {
        ui.motion.forEach(a => a.cancel());
        ui.motion = null;
        ui.closing = false;
        for (const key of ['position', 'left', 'right', 'width']) ui.node.style[key] = '';
      }
      ui.node.hidden = !d.open && !ui.closing;
      ui.node.dataset.locked = String(d.locked);
      ui.node.dataset.reserved = String(reserve[side]);
      ui.node.style.setProperty('--drawer-width', d.width + 'px');
      ui.lock.setAttribute('aria-pressed', String(d.locked));
      ui.lock.setAttribute(
        'aria-label',
        (d.locked ? 'Unlock ' : 'Lock ') +
          side +
          ' drawer' +
          (narrow && d.locked ? ' · locked as overlay' : '')
      );
      ui.lock.textContent = d.locked ? '▣' : '◇';
      ui.pull.textContent =
        this.registry.get(this.layout.panes[side].active)?.title ||
        (side === 'left' ? 'Toolboxes' : 'Ask');
      ui.pull.setAttribute('aria-expanded', String(d.open));
      ui.node.replaceChildren(this.renderPane(side), ui.resize);
      if (narrow && d.open) {
        const other = side === 'left' ? 'right' : 'left';
        if (
          this.layout.drawers[other].open &&
          document.activeElement &&
          ui.node.contains(document.activeElement)
        )
          this[other].node.hidden = true;
      }
    }
    if (narrow && this.layout.drawers.left.open && this.layout.drawers.right.open) {
      const side = this.overlaySide || 'right';
      this[side === 'right' ? 'left' : 'right'].node.hidden = true;
    }
    this.mobile.replaceChildren(
      ...leaves(this.layout.tree).map(id => {
        const o = el(
          'option',
          null,
          this.registry.get(this.layout.panes[id].active)?.title || 'Empty pane'
        );
        o.value = id;
        return o;
      })
    );
    this.mobile.value = this.layout.mobilePane;
    this.shelf.replaceChildren(
      ...Object.entries(this.layout.minimized)
        .filter(([, v]) => !v.preset)
        .map(([id]) =>
          button(
            (this.registry.get(this.layout.panes[id].active)?.title || 'Pane') +
              ` (${this.layout.panes[id].tabs.length})`,
            'Restore pane',
            () => this.dispatch({ type: 'restore', pane: id })
          )
        )
    );
    this.shelf.hidden = !this.shelf.childElementCount;
    this.rendering = false;
    this.onLayout?.(this.layout);
    this.updateViews();
    for (const side of ['left', 'right']) {
      const ui = this[side],
        before = drawerBefore[side];
      const visible = this.layout.drawers[side].open && (!ui.node.hidden || ui.closing);
      ui.visible = visible;
      if (visible === before.visible || matchMedia('(prefers-reduced-motion: reduce)').matches)
        continue;
      ui.motion?.forEach(a => a.cancel());
      const content = this.registry.get(this.layout.panes[side].active)?.element;
      const offset =
        (side === 'left' ? -1 : 1) * (before.rect.width || this.layout.drawers[side].width);
      if (!visible) {
        const root = this.root.getBoundingClientRect();
        ui.closing = true;
        ui.node.hidden = false;
        Object.assign(ui.node.style, {
          position: 'absolute',
          left: before.rect.left - root.left + 'px',
          right: 'auto',
          width: before.rect.width + 'px',
        });
        if (content) content.hidden = false;
      }
      const frames = visible
        ? [
            { transform: `translateX(${offset}px)`, opacity: 0 },
            { transform: 'translateX(0)', opacity: 1 },
          ]
        : [
            { transform: 'translateX(0)', opacity: 1 },
            { transform: `translateX(${offset}px)`, opacity: 0 },
          ];
      ui.motion = [ui.node, content]
        .filter(Boolean)
        .map(node => node.animate(frames, { duration: 180, easing: 'cubic-bezier(.2,.8,.2,1)' }));
      const motion = ui.motion;
      Promise.all(motion.map(a => a.finished.catch(() => {}))).then(() => {
        if (ui.motion !== motion) return;
        ui.motion = null;
        ui.closing = false;
        for (const key of ['position', 'left', 'right', 'width']) ui.node.style[key] = '';
        ui.node.hidden = !ui.visible;
        this.updateViews();
      });
    }
    requestAnimationFrame(() => this.updateViews());
    if (!this.observer) {
      this.observer = new ResizeObserver(() => this.updateViews());
      this.observer.observe(this.root);
    }
    for (const pane of this.panes.values()) this.observer.observe(pane);
  }
  updateViews() {
    const root = this.root.getBoundingClientRect();
    for (const [id, view] of this.registry) {
      const paneId = location(this.layout, id),
        pane = this.panes.get(paneId),
        body = pane?.querySelector('.pane-content');
      const visible =
        paneId &&
        this.layout.panes[paneId].active === id &&
        pane?.isConnected &&
        !pane.closest('[hidden]') &&
        !pane.parentElement?.closest('.view-parking');
      if (['left', 'right'].includes(paneId) && this[paneId].closing) continue;
      view.element.hidden = !visible;
      if (!visible) continue;
      const r = body.getBoundingClientRect();
      Object.assign(view.element.style, {
        left: r.left - root.left + 'px',
        top: r.top - root.top + 'px',
        width: r.width + 'px',
        height: r.height + 'px',
        zIndex: ['left', 'right'].includes(paneId) ? '51' : '2',
      });
    }
  }
  menu(pane, tab) {
    this.lastFocusedTab = null;
    this.menuDialog?.remove();
    const dialog = el('dialog', 'layout-menu');
    this.menuDialog = dialog;
    const title = el('h3', null, tab ? 'Move view' : 'Pane actions');
    dialog.append(title);
    const close = () => dialog.close();
    dialog.append(button('Close menu', null, close));
    for (const target of Object.keys(this.layout.panes).filter(id => !this.layout.minimized[id])) {
      const label = ['left', 'right'].includes(target)
        ? target + ' drawer'
        : this.registry.get(this.layout.panes[target].active)?.title || target;
      dialog.append(
        button('Move to ' + label, null, () => {
          this.dispatch({ type: 'move', ...(tab ? { tab } : { pane }), target }, { focus: true });
          close();
        })
      );
    }
    const splitTarget = ['left', 'right'].includes(pane) ? leaves(this.layout.tree)[0] : pane;
    for (const edge of ['left', 'right', 'top', 'bottom'])
      dialog.append(
        button('Split ' + edge, null, () => {
          if (!this.canSplit(splitTarget, edge))
            return this.announce('Not enough space to split. Merge with an existing pane.');
          this.dispatch(
            { type: 'move', ...(tab ? { tab } : { pane }), target: splitTarget, edge },
            { focus: true }
          );
          close();
        })
      );
    if (!tab && !['left', 'right'].includes(pane)) {
      dialog.append(
        button('Minimize pane', null, () => {
          this.dispatch({ type: 'minimize', pane });
          close();
        }),
        button('Maximize / restore', null, () => {
          this.dispatch({ type: 'maximize', pane });
          close();
        }),
        button('Close pane', null, () => {
          this.closePane(pane);
          close();
        })
      );
    }
    for (const [id, t] of this.registry)
      if ((!t.project || t.project === this.project) && !location(this.layout, id))
        dialog.append(
          button('Open ' + t.title, null, () => {
            this.open(id, pane);
            this.onActivate?.(id);
            close();
          })
        );
    document.body.append(dialog);
    dialog.showModal();
    dialog.addEventListener(
      'close',
      () => {
        if (this.lastFocusedTab) this.focus(this.lastFocusedTab);
        else if (this.panes.get(pane)?.closest('.view-parking'))
          this.shelf.querySelector('button')?.focus();
        else this.panes.get(pane)?.querySelector('.pane-menu')?.focus();
      },
      { once: true }
    );
  }
  minimum(node, axis) {
    if (node.pane) {
      const tabs = this.layout.panes[node.pane]?.tabs || [];
      return axis === 'y'
        ? 244
        : tabs.some(id => id === 'ask' || id === 'source' || id.startsWith('source:'))
          ? 320
          : 280;
    }
    const a = this.minimum(node.first, axis),
      b = this.minimum(node.second, axis);
    return node.axis === axis ? a + b + 8 : Math.max(a, b);
  }
  canSplit(pane, edge) {
    const r = this.panes.get(pane)?.getBoundingClientRect();
    return r && (['left', 'right'].includes(edge) ? r.width >= 648 : r.height >= 424);
  }
  beginDrag(event, payload) {
    if (event.button !== 0) return;
    const start = { x: event.clientX, y: event.clientY },
      target = event.currentTarget;
    let holding = event.pointerType !== 'touch',
      hold = setTimeout(() => (holding = true), 200);
    this.pressed = { payload, start };
    const move = e => {
      if (!this.drag) {
        if (!holding || Math.hypot(e.clientX - start.x, e.clientY - start.y) < 6) return;
        this.drag = {
          payload,
          before: structuredClone(this.layout),
          target: null,
          edge: null,
          edgeSince: 0,
        };
        this.ghost.textContent = payload.tab ? this.registry.get(payload.tab)?.title : 'Tab group';
        this.ghost.hidden = false;
        this.capture.hidden = false;
      }
      this.ghost.style.transform = `translate(${e.clientX + 12}px,${e.clientY + 12}px)`;
      this.capture.style.pointerEvents = 'none';
      const under = document.elementFromPoint(e.clientX, e.clientY),
        view = under?.closest('[data-view-id]'),
        pane = under?.closest('.dock-pane') || this.panes.get(view?.dataset.paneLocation),
        id = pane?.dataset.pane;
      this.capture.style.pointerEvents = 'auto';
      if (!id) {
        this.drag.target = null;
        this.preview.hidden = true;
        return;
      }
      const r = pane.getBoundingClientRect(),
        strip = under.closest('.tab-strip');
      let edge = null;
      if (!strip && !['left', 'right'].includes(id)) {
        const distances = {
          left: e.clientX - r.left,
          right: r.right - e.clientX,
          top: e.clientY - r.top,
          bottom: r.bottom - e.clientY,
        };
        const nearest = Object.entries(distances).sort((a, b) => a[1] - b[1])[0];
        const size = ['left', 'right'].includes(nearest[0]) ? r.width : r.height;
        if (nearest[1] < Math.max(48, size * 0.25)) edge = nearest[0];
      }
      if (this.drag.target !== id || this.drag.edge !== edge) {
        this.drag.edgeSince = performance.now();
        this.drag.edge = edge;
        clearTimeout(this.edgeTimer);
        if (edge)
          this.edgeTimer = setTimeout(() => {
            if (this.drag) move(e);
          }, 185);
      }
      this.drag.target = id;
      this.drag.index = strip
        ? [...strip.children].findIndex(
            c => e.clientX < c.getBoundingClientRect().left + c.clientWidth / 2
          )
        : null;
      if (this.drag.index === -1) this.drag.index = this.layout.panes[id].tabs.length;
      const allowed =
        edge && this.canSplit(id, edge) && performance.now() - this.drag.edgeSince >= 180;
      this.drag.previewEdge = allowed ? edge : null;
      let box = { x: r.left, y: r.top, w: r.width, h: r.height };
      if (allowed) {
        if (edge === 'left' || edge === 'right') {
          box.w /= 2;
          if (edge === 'right') box.x += box.w;
        } else {
          box.h /= 2;
          if (edge === 'bottom') box.y += box.h;
        }
      }
      if (strip) {
        const children = [...strip.children],
          at = children[this.drag.index],
          r = strip.getBoundingClientRect();
        box = {
          x: at ? at.getBoundingClientRect().left : r.right - 3,
          y: r.top,
          w: 3,
          h: r.height,
        };
      }
      this.preview.classList.toggle('insertion', !!strip);
      this.preview.hidden = false;
      Object.assign(this.preview.style, {
        left: box.x + 'px',
        top: box.y + 'px',
        width: box.w + 'px',
        height: box.h + 'px',
      });
      this.preview.textContent = strip
        ? ''
        : allowed
          ? 'Split ' + edge
          : edge && !this.canSplit(id, edge)
            ? 'Merge · split would be too small'
            : 'Merge tabs';
    };
    const end = () => {
      if (this.drag)
        target.addEventListener(
          'click',
          e => {
            e.preventDefault();
            e.stopImmediatePropagation();
          },
          { capture: true, once: true }
        );
      clearTimeout(hold);
      removeEventListener('pointermove', move);
      removeEventListener('pointerup', end);
      removeEventListener('pointercancel', cancel);
      const drag = this.drag;
      this.cancelDrag();
      if (drag?.target) {
        this.layout = drag.before;
        this.dispatch(
          {
            type: 'move',
            ...payload,
            target: drag.target,
            ...(drag.previewEdge ? { edge: drag.previewEdge } : {}),
            ...(drag.index !== null ? { index: drag.index } : {}),
          },
          { focus: true }
        );
        this.announce('View moved.');
      }
    };
    const cancel = () => {
      clearTimeout(hold);
      removeEventListener('pointermove', move);
      removeEventListener('pointerup', end);
      removeEventListener('pointercancel', cancel);
      this.cancelDrag();
    };
    addEventListener('pointermove', move);
    addEventListener('pointerup', end);
    addEventListener('pointercancel', cancel);
  }
  cancelDrag() {
    if (this.drag) {
      this.layout = this.drag.before;
      this.drag = null;
      this.render();
    }
    this.ghost.hidden = true;
    this.preview.hidden = true;
    this.capture.hidden = true;
    clearTimeout(this.hoverTimer);
    clearTimeout(this.edgeTimer);
  }
}
