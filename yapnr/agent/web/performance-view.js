const el = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};
export function mountPerformance(container, { project, report } = {}) {
  container.replaceChildren();
  container.classList.add('performance-view');
  const native = el('section'),
    cloud = el('section'),
    status = el('p'),
    form = el('form');
  native.className = 'native-performance';
  cloud.className = 'cloud-settings';
  cloud.append(
    el('h3', 'Cloud execution'),
    el(
      'p',
      'Settings apply to this project. Credentials stay on the server host; the execution environment needs its own SDK access. Saving does not launch jobs.'
    ),
    status
  );
  const provider = el('select'),
    option = el('option', 'Google Cloud Batch');
  option.value = 'gcp-batch';
  provider.append(option);
  const providerLabel = el('label', 'Provider');
  providerLabel.append(provider);
  form.append(providerLabel);
  const profile = el('select');
  profile.setAttribute('aria-label', 'gcloud configuration');
  const profileLabel = el('label', 'gcloud configuration');
  profileLabel.append(profile);
  form.append(profileLabel);
  const fields = {};
  for (const [key, label] of [
    ['project', 'Project ID'],
    ['home_region', 'Home region'],
    ['regions', 'Allowed regions (comma separated)'],
    ['inputs_bucket', 'Input bucket'],
    ['runs_bucket', 'Run bucket'],
    ['submit_service_account', 'Submit service account ID'],
    ['runner_service_account', 'Runner service account ID'],
  ]) {
    const input = el('input');
    input.name = key;
    input.setAttribute('aria-label', label);
    fields[key] = input;
    const row = el('label', label);
    row.append(input);
    form.append(row);
  }
  const save = el('button', 'Save project cloud settings'),
    refresh = el('button', 'Refresh host authentication');
  save.disabled = true;
  refresh.type = 'button';
  save.type = 'submit';
  form.append(save, refresh);
  cloud.append(form);
  container.append(native, cloud);
  let current,
    disposed = false,
    profiles = [];
  async function load() {
    try {
      const response = await fetch('/yapnr/api/cloud/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Cloud settings unavailable');
      const value = await response.json();
      if (disposed) return;
      current = value;
      const gcp = current.settings.gcp || {};
      for (const [key, input] of Object.entries(fields))
        input.value = Array.isArray(gcp[key]) ? gcp[key].join(', ') : gcp[key] || '';
      if (!fields.home_region.value) fields.home_region.value = 'us-central1';
      status.textContent = `${
        current.scope === 'project' ? 'Project' : 'Operator defaults'
      } · Google Cloud Batch is supported. Additional cloud providers appear when supported.`;
      await authentication();
    } catch (error) {
      status.textContent = error.message;
      report?.(error.message);
    }
  }
  async function authentication() {
    refresh.disabled = true;
    try {
      const response = await fetch('/yapnr/api/cloud-sdk/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Host authentication unavailable');
      const sdk = await response.json();
      if (disposed) return;
      profiles = sdk.profiles || [];
      profile.replaceChildren(el('option', 'Host default'));
      profile.firstChild.value = '';
      for (const item of profiles) {
        const option = el('option', item.name + (item.active ? ' · active' : ''));
        option.value = item.name;
        profile.append(option);
      }
      profile.value = current?.settings.gcp?.gcloud_configuration || '';
      status.textContent +=
        ' ' +
        sdk.message +
        (sdk.accounts?.length ? ` ${sdk.accounts.length} authenticated account(s) recorded.` : '');
    } catch (error) {
      report?.(error.message);
    } finally {
      refresh.disabled = false;
      save.disabled = !current;
    }
  }
  profile.onchange = () => {
    const item = profiles.find(p => p.name === profile.value);
    if (item?.properties?.core?.project) fields.project.value = item.properties.core.project;
  };
  refresh.onclick = authentication;
  form.onsubmit = async event => {
    event.preventDefault();
    if (!current) return;
    save.disabled = true;
    try {
      const settings = structuredClone(current.settings);
      settings.gcp ||= {};
      for (const [key, input] of Object.entries(fields)) {
        if (key === 'regions')
          settings.gcp[key] = input.value
            .split(',')
            .map(v => v.trim())
            .filter(Boolean);
        else if (input.value.trim()) settings.gcp[key] = input.value.trim();
        else delete settings.gcp[key];
      }
      if (!settings.gcp.regions.length) settings.gcp.regions = [settings.gcp.home_region];
      if (profile.value) settings.gcp.gcloud_configuration = profile.value;
      else delete settings.gcp.gcloud_configuration;
      const response = await fetch('/yapnr/api/cloud/' + encodeURIComponent(project), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings, sha256: current.sha256 }),
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || 'Cloud configuration failed');
      current = result;
      status.textContent =
        'Saved project cloud settings. Future experiment commands in this workspace use this profile. No job launched.';
      report?.('Project cloud settings saved.');
    } catch (error) {
      status.textContent = error.message;
    } finally {
      save.disabled = false;
    }
  };
  load();
  return {
    native,
    dispose() {
      disposed = true;
    },
  };
}
