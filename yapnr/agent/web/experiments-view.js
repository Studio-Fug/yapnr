const el = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};
export function mountExperiments(container, { project, open, report } = {}) {
  container.replaceChildren();
  container.classList.add('experiment-browser');
  const history = el('section'),
    native = el('section');
  native.className = 'native-experiments';
  native.style.height = '480px';
  native.style.minHeight = '240px';
  history.className = 'build-history';
  history.append(el('h3', 'Recorded build attempts'));
  container.append(history, native);
  let disposed = false,
    key = '',
    pending = false;
  async function update() {
    if (disposed || pending) return;
    pending = true;
    try {
      const response = await fetch('/yapnr/api/experiments/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Experiment history unavailable');
      const data = await response.json();
      if (disposed || JSON.stringify(data) === key) return;
      key = JSON.stringify(data);
      history.replaceChildren(el('h3', 'Recorded build attempts'));
      if (!data.runs.length) history.append(el('p', 'No schematic-build attempts recorded.'));
      for (const run of data.runs) {
        const details = el('details'),
          summary = el('summary', `${run.id} · ${run.status}`);
        details.append(
          summary,
          el(
            'p',
            `${run.kind}${run.seconds == null ? '' : ' · ' + run.seconds + ' s'}${
              run.catalog_parts == null ? '' : ' · ' + run.catalog_parts + ' catalog candidates'
            }`
          )
        );
        const result = el('button', 'Open recorded result');
        result.onclick = () => open(run.path);
        details.append(result);
        history.append(details);
      }
    } catch (error) {
      report?.(error.message);
    } finally {
      pending = false;
    }
  }
  update();
  const timer = setInterval(update, 3000);
  return {
    native,
    dispose() {
      disposed = true;
      clearInterval(timer);
    },
  };
}
