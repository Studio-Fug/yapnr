/* Layout transactions contain view references only; never project/backend mutations. */
const copy = value => structuredClone(value);
export const VERSION = 1;
export function initialLayout() {
  return {
    version: VERSION,
    tree: {
      axis: 'x',
      ratio: 0.58,
      first: { pane: 'board' },
      second: { axis: 'y', ratio: 0.55, first: { pane: 'schematic' }, second: { pane: 'source' } },
    },
    panes: {
      board: { tabs: ['board', 'three'], active: 'board' },
      schematic: { tabs: ['schematic'], active: 'schematic' },
      source: { tabs: ['source'], active: 'source' },
      left: {
        tabs: ['source-browser', 'experiments', 'controls', 'exploration'],
        active: 'source-browser',
      },
      right: { tabs: ['ask', 'inspect', 'notes', 'timing', 'activity'], active: 'ask' },
    },
    drawers: {
      left: { open: false, locked: false, width: 320 },
      right: { open: true, locked: true, width: 380 },
    },
    minimized: {},
    closed: [],
    mobilePane: 'board',
    maximized: null,
    serial: 0,
    preset: 'inspect',
  };
}
export function leaves(tree) {
  return tree.pane ? [tree.pane] : [...leaves(tree.first), ...leaves(tree.second)];
}
export function location(layout, tab) {
  return Object.keys(layout.panes).find(p => layout.panes[p].tabs.includes(tab));
}
function replace(tree, id, replacement) {
  if (tree.pane) return tree.pane === id ? copy(replacement) : tree;
  return {
    ...tree,
    first: replace(tree.first, id, replacement),
    second: replace(tree.second, id, replacement),
  };
}
function remove(tree, id) {
  if (tree.pane) return tree.pane === id ? null : tree;
  const first = remove(tree.first, id),
    second = remove(tree.second, id);
  return first && second ? { ...tree, first, second } : first || second;
}
function anchor(tree, id, result = null) {
  if (tree.pane) return result;
  if (leaves(tree.first).includes(id))
    return anchor(tree.first, id, {
      sibling: leaves(tree.second)[0],
      edge: tree.axis === 'x' ? 'left' : 'top',
      ratio: tree.ratio,
    });
  return anchor(tree.second, id, {
    sibling: leaves(tree.first)[0],
    edge: tree.axis === 'x' ? 'right' : 'bottom',
    ratio: tree.ratio,
  });
}
function collapse(layout, id) {
  if (id === 'left' || id === 'right' || layout.panes[id].tabs.length) return;
  if (leaves(layout.tree).length > 1) {
    layout.tree = remove(layout.tree, id);
    delete layout.panes[id];
  }
}
export function validate(value) {
  if (!value || value.version !== VERSION || !value.tree || !value.panes || !value.drawers)
    throw Error('Unsupported layout');
  const found = new Set(),
    paneIds = new Set();
  function tree(node) {
    if (node.pane) {
      if (
        paneIds.has(node.pane) ||
        !value.panes[node.pane] ||
        ['left', 'right'].includes(node.pane)
      )
        throw Error('Invalid pane');
      paneIds.add(node.pane);
      return;
    }
    if (!['x', 'y'].includes(node.axis) || !(node.ratio > 0 && node.ratio < 1))
      throw Error('Invalid split');
    tree(node.first);
    tree(node.second);
  }
  tree(value.tree);
  for (const [id, p] of Object.entries(value.panes)) {
    if (!Array.isArray(p.tabs) || (p.tabs.length && !p.tabs.includes(p.active)))
      throw Error('Invalid tabs');
    for (const t of p.tabs) {
      if (typeof t !== 'string' || found.has(t)) throw Error('Duplicate tab');
      found.add(t);
    }
    if (!['left', 'right'].includes(id) && !leaves(value.tree).includes(id) && !value.minimized[id])
      throw Error('Orphan pane');
  }
  return value;
}
export function transact(previous, action) {
  const l = copy(previous),
    type = action.type;
  if (type === 'activate') {
    const p = location(l, action.tab);
    if (!p) throw Error('Missing tab');
    l.panes[p].active = action.tab;
    if (p === 'left' || p === 'right') l.drawers[p].open = true;
    else l.mobilePane = p;
  } else if (type === 'open') {
    const existing = location(l, action.tab);
    const p = existing || action.pane || leaves(l.tree)[0];
    if (!l.panes[p]) throw Error('Missing target pane');
    if (!existing) l.panes[p].tabs.push(action.tab);
    l.panes[p].active = action.tab;
    l.closed = l.closed.filter(t => t !== action.tab);
    if (p === 'left' || p === 'right') l.drawers[p].open = true;
    else l.mobilePane = p;
  } else if (type === 'move') {
    const origin = action.pane || location(l, action.tab),
      dest = action.target;
    if (!l.panes[origin] || !l.panes[dest]) throw Error('Missing move target');
    const tabs = action.pane ? [...l.panes[origin].tabs] : [action.tab];
    const active = action.pane ? l.panes[origin].active : action.tab;
    if (!tabs.length) return l;
    if (origin === dest && action.edge && tabs.length === l.panes[origin].tabs.length)
      throw Error('Split needs another destination or another tab in the source group');
    l.panes[origin].tabs = l.panes[origin].tabs.filter(t => !tabs.includes(t));
    if (!l.panes[origin].tabs.includes(l.panes[origin].active))
      l.panes[origin].active = l.panes[origin].tabs[0] || null;
    let target = dest;
    if (action.edge) {
      if (dest === 'left' || dest === 'right') throw Error('Drawers accept tab merges only');
      target = 'pane-' + ++l.serial;
      l.panes[target] = { tabs: [], active: null };
      const leading = ['left', 'top'].includes(action.edge);
      l.tree = replace(l.tree, dest, {
        axis: ['left', 'right'].includes(action.edge) ? 'x' : 'y',
        ratio: 0.5,
        first: { pane: leading ? target : dest },
        second: { pane: leading ? dest : target },
      });
    }
    const index = Math.min(
      Math.max(action.index ?? l.panes[target].tabs.length, 0),
      l.panes[target].tabs.length
    );
    l.panes[target].tabs.splice(index, 0, ...tabs);
    l.panes[target].active = active;
    collapse(l, origin);
    if (target === 'left' || target === 'right') l.drawers[target].open = true;
    else l.mobilePane = target;
  } else if (type === 'close') {
    const p = location(l, action.tab);
    if (!p) return l;
    l.panes[p].tabs = l.panes[p].tabs.filter(t => t !== action.tab);
    if (l.panes[p].active === action.tab) l.panes[p].active = l.panes[p].tabs[0] || null;
    l.closed = [action.tab, ...l.closed.filter(t => t !== action.tab)].slice(0, 30);
    collapse(l, p);
  } else if (type === 'minimize') {
    if (leaves(l.tree).length === 1 || ['left', 'right'].includes(action.pane))
      throw Error('Keep one central pane open');
    l.minimized[action.pane] = anchor(l.tree, action.pane);
    l.tree = remove(l.tree, action.pane);
    if (l.maximized === action.pane) l.maximized = null;
  } else if (type === 'restore') {
    const slot = l.minimized[action.pane];
    if (!slot) return l;
    const target = leaves(l.tree).includes(slot.sibling) ? slot.sibling : leaves(l.tree)[0];
    const leading = ['left', 'top'].includes(slot.edge);
    l.tree = replace(l.tree, target, {
      axis: ['left', 'right'].includes(slot.edge) ? 'x' : 'y',
      ratio: slot.ratio,
      first: { pane: leading ? action.pane : target },
      second: { pane: leading ? target : action.pane },
    });
    delete l.minimized[action.pane];
    l.mobilePane = action.pane;
  } else if (type === 'drawer') Object.assign(l.drawers[action.side], action.values);
  else if (type === 'resize') {
    let node = l.tree;
    for (const key of action.path || []) node = node[key];
    if (node.pane) throw Error('Not a splitter');
    node.ratio = Math.max(0.1, Math.min(0.9, action.ratio));
  } else if (type === 'maximize') l.maximized = l.maximized === action.pane ? null : action.pane;
  else if (type === 'mobile') l.mobilePane = action.pane;
  else if (type === 'preset') {
    if (action.name === l.preset) return l;
    if (action.name === 'conversation') {
      l.inspectTree = copy(l.tree);
      const from = location(l, 'ask');
      l.askSlot = { pane: from, index: from ? l.panes[from].tabs.indexOf('ask') : 0 };
      if (from) l.panes[from].tabs = l.panes[from].tabs.filter(t => t !== 'ask');
      if (from && l.panes[from].active === 'ask')
        l.panes[from].active = l.panes[from].tabs[0] || null;
      l.panes.conversation = { tabs: ['ask'], active: 'ask' };
      l.tree = { pane: 'conversation' };
      for (const p of leaves(l.inspectTree))
        if (p !== 'conversation') l.minimized[p] = { preset: true };
      l.drawers.right.open = false;
      l.mobilePane = 'conversation';
    } else if (l.inspectTree) {
      const askFrom = location(l, 'ask');
      if (askFrom) l.panes[askFrom].tabs = l.panes[askFrom].tabs.filter(t => t !== 'ask');
      l.tree = l.inspectTree;
      delete l.inspectTree;
      for (const p of leaves(l.tree)) delete l.minimized[p];
      const extras = l.panes.conversation?.tabs || [];
      const target = leaves(l.tree)[0];
      l.panes[target].tabs.push(...extras);
      delete l.panes.conversation;
      if (!location(l, 'ask')) {
        const dest = l.panes[l.askSlot?.pane] ? l.askSlot.pane : 'right';
        l.panes[dest].tabs.splice(l.askSlot?.index || 0, 0, 'ask');
        l.panes[dest].active = 'ask';
      }
      delete l.askSlot;
      l.drawers.right.open = true;
      for (const p of Object.keys(l.panes))
        if (!l.panes[p].tabs.includes(l.panes[p].active))
          l.panes[p].active = l.panes[p].tabs[0] || null;
    }
    l.preset = action.name;
    l.maximized = null;
  } else if (type === 'reset') return initialLayout();
  else throw Error('Unknown layout action');
  if (!leaves(l.tree).includes(l.mobilePane)) l.mobilePane = leaves(l.tree)[0];
  return validate(l);
}
