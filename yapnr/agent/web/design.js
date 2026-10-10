/* Live source and netlist surface. Geometry is mechanically derived, never model-invented. */
let elkReady;
function loadElk() {
  if (!elkReady) elkReady = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/yapnr/vendor/elk.bundled.js';
    script.onload = () => resolve(new window.ELK());
    script.onerror = () => { elkReady = null; reject(Error('Bundled schematic layout engine failed to load.')); };
    document.head.append(script);
  });
  return elkReady;
}
function element(tag, text) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  return e;
}
function svgElement(tag, attrs, parent, text) {
  const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) e.setAttribute(key, value);
  if (text !== undefined) e.textContent = text;
  parent.append(e);
  return e;
}
async function schematic(item) {
  const elk = await loadElk();
  const children = [], ports = new Map();
  for (const [i, c] of item.graph.components.entries()) {
    const id = 'c' + i, pins = c.pads || [];
    children.push({id, width: 210, height: Math.max(75, 55 + pins.length * 19),
      layoutOptions: {'elk.portConstraints': 'FIXED_POS'},
      ports: pins.map((p, j) => {
        const port = id + 'p' + j;
        ports.set(c.ref + '\0' + p.name, port);
        return {id: port, x: 210, y: 45 + j * 19, width: 1, height: 1};
      }), component: c});
  }
  const edges = [];
  for (const [i, net] of item.graph.nets.entries()) {
    const pins = [...new Set((net.pins || []).map(([ref, pin]) => ports.get(ref + '\0' + pin)).filter(Boolean))];
    for (let j = 1; j < pins.length; j++) edges.push({id: 'n' + i + 'e' + j, sources: [pins[0]], targets: [pins[j]], net: net.name});
  }
  const layout = await elk.layout({id: 'root', children, edges, layoutOptions: {
    'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.edgeRouting': 'ORTHOGONAL',
    'elk.randomSeed': '1', 'elk.spacing.nodeNode': '35', 'elk.layered.spacing.nodeNodeBetweenLayers': '80'
  }});
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${layout.width + 20} ${layout.height + 20}`);
  svg.setAttribute('aria-label', 'Live schematic connectivity');
  for (const edge of layout.edges || []) {
    const group = svgElement('g', {}, svg);
    svgElement('title', {}, group, edge.net);
    for (const section of edge.sections || []) {
      const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint];
      svgElement('polyline', {points: points.map(p => `${p.x},${p.y}`).join(' '), fill: 'none', stroke: '#62b3ee', 'stroke-width': 1.5}, group);
    }
  }
  for (const child of layout.children || []) {
    const c = child.component;
    const g = svgElement('g', {transform: `translate(${child.x},${child.y})`}, svg);
    svgElement('rect', {width: child.width, height: child.height, rx: 5, fill: '#182330', stroke: '#7f9bb5'}, g);
    svgElement('text', {x: 10, y: 20, fill: '#edf4ff', 'font-size': 14}, g, c.ref);
    svgElement('text', {x: 10, y: 36, fill: '#afc0d3', 'font-size': 10}, g, c.value || c.footprint || c.address || '');
    for (const [i, pin] of (c.pads || []).entries()) {
      svgElement('text', {x: 10, y: 49 + i * 19, fill: '#dce5ef', 'font-size': 11}, g, `${pin.name} · ${pin.net || 'unconnected'}`);
    }
  }
  return svg;
}
export function mountDesign(container, project, viewerUrl) {
  container.replaceChildren();
  const wrap = element('div'); wrap.className = 'live-design';
  const sources = element('section'), output = element('section');
  const select = element('select'), code = element('pre'), status = element('p');
  select.setAttribute('aria-label', 'Live atopile source');
  code.setAttribute('aria-label', 'Atopile source being written');
  sources.append(element('h3', 'Atopile sources · live'), select, code);
  output.append(element('h3', 'Schematic · live'), status);
  const drawing = element('div'); drawing.className = 'live-schematic'; output.append(drawing);
  if (viewerUrl) {
    const frame = element('iframe');
    const url = new URL(viewerUrl, location.href); url.searchParams.set('workspace', '1'); url.searchParams.set('view', 'sch');
    frame.src = url.href; frame.title = project + ' engineering workspace';
    drawing.append(frame);
  }
  wrap.append(sources, output); container.append(wrap);
  let disposed = false, pending = false, files = [], sourceKey = '', graphKey = '', serial = 0;
  const showSource = () => { const text = files.find(f => f.path === select.value)?.text || 'Waiting for the agent to write the first .ato source.'; if (code.textContent !== text) code.textContent = text; };
  select.onchange = showSource;
  async function update() {
    if (disposed || pending || !wrap.isConnected) return;
    pending = true;
    try {
      const response = await fetch('/yapnr/api/design/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Live design connection error');
      const data = await response.json();
      if (disposed) return;
      files = data.sources;
      const key = JSON.stringify(files.map(f => [f.path, f.sha256]));
      if (key !== sourceKey) {
        const selected = select.value;
        select.replaceChildren(...files.map(f => { const o = element('option', f.path); o.value = f.path; return o; }));
        if (files.some(f => f.path === selected)) select.value = selected;
        showSource(); sourceKey = key;
      }
      if (!files.length) showSource();
      status.textContent = data.schematic
        ? `${data.schematic.path} · ${data.schematic.sha256.slice(0, 12)} · exported connectivity; generic component symbols`
        : data.reason + (graphKey ? ' Showing the last valid schematic.' : '');
      if (data.source_newer) status.textContent += ' · sources changed; awaiting a new successful netlist export';
      if (!viewerUrl && data.schematic && data.schematic.sha256 !== graphKey) {
        const token = ++serial, svg = await schematic(data.schematic);
        if (!disposed && token === serial) { drawing.replaceChildren(svg); graphKey = data.schematic.sha256; }
      }
    } catch (error) { if (!disposed) status.textContent = error.message + (graphKey ? ' · showing last valid schematic' : ''); }
    finally { pending = false; }
  }
  update();
  const timer = setInterval(update, 1000);
  return () => { disposed = true; serial++; clearInterval(timer); };
}
