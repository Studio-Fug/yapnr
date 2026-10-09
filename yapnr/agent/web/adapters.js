/* Persistent view instances adapt the existing renderer; docking never reloads them. */
export function mountViewer(container, url, mode, scope = 'Live / graph revision unavailable') {
  container.dataset.type = mode;
  container.dataset.scope = scope;
  if (!url) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'No ' + mode + ' artifact is attached to this project yet.';
    container.replaceChildren(p);
    return null;
  }
  const target = new URL(url, location.href);
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password)
    throw Error('Invalid renderer URL');
  target.searchParams.set('workspace', '1');
  target.searchParams.set('embed', mode);
  let frame = container.querySelector('iframe');
  if (!frame || frame.src !== target.href) {
    frame = document.createElement('iframe');
    frame.src = target.href;
    frame.title = 'yapnr ' + mode;
    frame.dataset.mode = mode;
    frame.addEventListener('load', () => (frame.dataset.loaded = '1'));
    container.replaceChildren(frame);
  }
  return frame;
}
export function sendView(frame, type, payload = {}) {
  if (frame?.contentWindow)
    frame.contentWindow.postMessage({ type, ...payload }, new URL(frame.src).origin);
}
