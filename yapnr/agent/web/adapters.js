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
  target.searchParams.delete('theme');
  let frame = container.querySelector('iframe');
  const existing = frame ? new URL(frame.src) : null;
  existing?.searchParams.delete('theme');
  if (!frame || existing.href !== target.href) {
    target.searchParams.set(
      'theme',
      document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
    );
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
