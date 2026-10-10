const el = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};
const link = (project, id, title) => {
  const node = el('a', title);
  node.href = `/yapnr/artifact/${encodeURIComponent(project)}/${id}`;
  node.download = '';
  return node;
};
const field = (title, input) => {
  const label = el('label');
  label.append(el('span', title), input);
  return label;
};
const select = choices => {
  const node = el('select');
  for (const [value, title] of choices) {
    const option = el('option', title);
    option.value = value;
    node.append(option);
  }
  return node;
};
function tableView(title, data) {
  const details = el('details'), summary = el('summary', `${title} · ${data.rows.length} rows`);
  const wrap = el('div'), table = el('table'), header = el('tr');
  wrap.className = 'assembly-table-scroll';
  for (const name of data.columns) header.append(el('th', name));
  const head = el('thead'); head.append(header); table.append(head);
  const body = el('tbody');
  for (const cells of data.rows) {
    const row = el('tr');
    for (const cell of cells) row.append(el('td', cell));
    body.append(row);
  }
  table.append(body); wrap.append(table); details.append(summary, wrap);
  return details;
}
export function mountManufacturing(container, {project, approve, review, artifact, report, request} = {}) {
  let disposed = false, pending = false, key = '', preparing = false;
  const draft = {artifact: '', vendor: 'jlcpcb', quantity: 5, finish: '', throughHole: false};
  container.classList.add('manufacturing-browser');
  const previews = (panel, ids) => {
    if (!ids?.length) return;
    const details = el('details'); details.append(el('summary', 'Gerber previews · top / bottom'));
    for (const id of ids) {
      const image = el('img');
      image.src = `/yapnr/artifact/${encodeURIComponent(project)}/${id}`;
      image.alt = 'Published Gerber preview'; image.loading = 'lazy';
      image.onclick = () => artifact(id);
      details.append(image);
    }
    panel.append(details);
  };
  async function update() {
    if (disposed || pending) return;
    pending = true;
    try {
      const response = await fetch('/yapnr/api/manufacturing/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Manufacturing handoff unavailable');
      const data = await response.json();
      if (disposed || JSON.stringify(data) === key) return;
      key = JSON.stringify(data);
      container.replaceChildren(el('h2', 'Manufacturing'));
      const steps = el('ol'); steps.className = 'assembly-stages';
      for (const text of ['Select package', 'Choose supplier', 'Review files', 'Vendor quote & checkout']) steps.append(el('li', text));
      container.append(steps);
      const candidates = data.candidates || [];
      if (candidates.length) {
        const panel = el('section'); panel.className = 'assembly-release';
        panel.append(el('h3', '1. Prepared packages'));
        if (!candidates.some(c => c.artifact === draft.artifact)) draft.artifact = candidates.at(-1).artifact;
        const packages = select(candidates.map(c => [c.artifact, c.title])); packages.value = draft.artifact;
        packages.onchange = () => {draft.artifact = packages.value; key = ''; update();};
        panel.append(field('Package', packages));
        const candidate = candidates.find(c => c.artifact === draft.artifact);
        if (candidate.error) {panel.append(el('strong', candidate.error)); container.append(panel);}
        else {
        panel.append(link(project, candidate.artifact, 'Download original package'), el('p', candidate.qualification));
        panel.append(el('p', `Packaged DRC report: ${candidate.drc_errors} errors · ${candidate.unconnected} unconnected. Board ${candidate.board_sha256.slice(0, 12)}.`));
        const source = candidate.boards.find(b => b.current);
        if (source) {
          const inspect = el('button', 'Inspect source PCB'); inspect.onclick = () => artifact(source.artifact); panel.append(inspect);
        }
        if (!candidate.current) panel.append(el('p', 'Changed requirements/board, or missing source board. Regenerate this package before review.'));
        previews(panel, candidate.previews);
        panel.append(tableView('Complete BOM', candidate.bom), tableView('Prepared placement', candidate.cpl));
        const notes = el('details'); notes.append(el('summary', 'Assembly / verification notes'), el('pre', candidate.notes)); panel.append(notes);
        panel.append(el('h3', '2. Choose supplier & quote settings'));
        const vendor = select([['jlcpcb', 'JLCPCB'], ['pcbway', 'PCBWay']]); vendor.value = draft.vendor;
        const quantity = el('input'); quantity.type = 'number'; quantity.min = '1'; quantity.max = '10000'; quantity.step = '1'; quantity.required = true; quantity.value = draft.quantity;
        const finish = select([['', 'Choose finish…'], ['LeadFree HASL', 'Lead-free HASL'], ['ENIG', 'ENIG']]); finish.value = draft.finish;
        if (candidate.kind === 'qualified') {
          vendor.value = candidate.vendor; quantity.value = candidate.quantity;
          if (![...finish.options].some(o => o.value === candidate.finish)) finish.append(new Option(candidate.finish, candidate.finish));
          finish.value = candidate.finish; vendor.disabled = quantity.disabled = finish.disabled = true;
          panel.append(el('p', 'Supplier/settings are bound to this qualified package. Rebuild it to change them.'));
        }
        panel.append(field('Supplier', vendor), field('Requested PCB quantity', quantity), field('Requested finish', finish));
        const throughHole = el('input'); throughHole.type = 'checkbox'; throughHole.checked = draft.throughHole;
        throughHole.onchange = () => {draft.throughHole = throughHole.checked;};
        if (candidate.kind !== 'qualified') panel.append(field('Request vendor through-hole assembly (e.g. headers), subject to quote confirmation', throughHole));
        panel.append(el('p', 'The vendor confirms available options, quantity, stock and price. Prototype files have pending DFM/rotation checks; review these on the vendor site. Separate THT/hand assembly is listed in the complete BOM and notes.'));
        const prepare = el('button', 'Prepare supplier files for review');
        prepare.className = 'primary';
        const valid = () => {prepare.disabled = preparing || !candidate.current || candidate.drc_errors > 0 || candidate.unconnected > 0 || !finish.value || !quantity.checkValidity();};
        vendor.onchange = () => {draft.vendor = vendor.value; valid();};
        quantity.oninput = () => {draft.quantity = quantity.value; valid();};
        finish.onchange = () => {draft.finish = finish.value; valid();};
        prepare.onclick = async () => {
          preparing = true; prepare.disabled = true; prepare.textContent = 'Preparing local review packet…';
          try {
            const response = await fetch('/yapnr/api/assembly-package/' + encodeURIComponent(project), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({artifact: candidate.artifact, vendor: vendor.value, quantity: Number(quantity.value), finish: finish.value, include_through_hole: candidate.kind !== 'qualified' && throughHole.checked})});
            if (!response.ok) throw Error((await response.json()).error || 'Package preparation failed');
            key = '';
          } catch (e) { report(e.message); }
          finally {preparing = false; prepare.textContent = 'Prepare supplier files for review'; valid(); await update();}
        };
        valid(); panel.append(prepare); container.append(panel);
        }
      } else if (!data.releases.length) {
        container.append(el('p', 'No published manufacturing package yet. Prepared packages appear here automatically.'));
        if (request) {
          const start = el('button', 'Prepare manufacturing package'); start.className = 'primary'; start.onclick = request; container.append(start);
        }
      }
      for (const release of data.releases) {
        const panel = el('section'), card = release.card;
        panel.className = 'assembly-release';
        panel.append(el('h3', `3. Review · ${card.vendor.title}`), el('p', release.current ? release.review.replaceAll('_', ' ') : 'Changed inputs — prepare and review a new package'));
        panel.append(el('p', `${card.board.name} · ${card.board.size_mm.join(' × ')} mm · ${card.board.layers.length} layers · quantity ${card.qty.value}${card.options?.finish ? ' · ' + card.options.finish : ''}`));
        panel.append(el('p', `${card.profile.title} · ${card.stackup.summary}`));
        if (release.qualification) panel.append(el('strong', release.qualification));
        panel.append(el('p', `Packaged native DRC: ${card.checks.drc_errors} errors, ${card.checks.drc_warnings} warnings. ${release.prototype ? 'Vendor qualification remains open.' : `Fabrication: ${card.checks.summary.error} errors, ${card.checks.summary.warning} warnings.`}`));
        for (const note of [...(card.checks.notable || []), ...(card.assembly.notes || [])]) panel.append(el('p', note));
        previews(panel, release.previews);
        if (release.bom) panel.append(tableView('Supplier BOM', release.bom));
        if (release.cpl) panel.append(tableView('Supplier placement', release.cpl));
        const files = el('ul');
        for (const file of release.downloads) {const row = el('li'); row.append(link(project, file.artifact, file.label)); files.append(row);}
        panel.append(files, el('p', release.open_verification));
        const inspect = el('button', 'Inspect package & leave feedback'); inspect.onclick = () => artifact(release.artifact); panel.append(inspect);
        const confirm = el('label'), checkbox = el('input'); checkbox.type = 'checkbox';
        confirm.append(checkbox, document.createTextNode(' I reviewed the board revision, BOM, placement/polarity, separate assembly and outstanding checks.'));
        panel.append(confirm);
        const accept = el('button', release.prototype ? 'Approve prototype files for quote' : 'Approve package for vendor handoff'); accept.disabled = true;
        checkbox.onchange = () => { accept.disabled = !checkbox.checked || !release.current || release.feedback.length > 0 || release.handoff_ready; };
        accept.onclick = async () => {accept.disabled = true; try {await approve(release.artifact); key = ''; await update();} catch (e) {report(e.message); key = ''; await update();}};
        const feedback = el('button', 'Review Feedback'); feedback.disabled = !release.feedback.length; feedback.onclick = () => review(release);
        panel.append(accept, feedback, el('h3', '4. Vendor quote & checkout'));
        const vendor = el('a', release.handoff_ready ? 'Open ' + card.vendor.title + ' quote' : 'Approve the file review to enable vendor handoff');
        if (release.handoff_ready) {vendor.href = release.vendor_page; vendor.target = '_blank'; vendor.rel = 'noopener noreferrer';} else vendor.setAttribute('aria-disabled', 'true');
        panel.append(vendor);
        const instructions = el('ol');
        for (const text of ['Download Gerbers/drills and upload that ZIP on the vendor site.', 'Select PCB layers, dimensions, thickness, finish and quantity from this card; enable assembly.', 'Upload the supplier BOM and CPL separately. Check part matching, substitutions/stock, polarity and every rotation in the vendor preview. Confirm any requested through-hole service, or arrange the separate THT/hand assembly listed in the notes.', 'Resolve vendor DFM and review the quote and lead time before you complete checkout. Uploading and paying happen on the vendor site.']) instructions.append(el('li', text));
        panel.append(instructions, el('p', 'This app does not upload, place an order or pay. Approval of prototype files does not qualify the design or close physical verification requirements.'));
        const help = el('a', 'Official vendor upload instructions'); help.target = '_blank'; help.rel = 'noopener noreferrer';
        help.href = card.vendor.id === 'jlcpcb' ? 'https://jlcpcb.com/help/article/how-do-i-place-a-pcba-order' : 'https://www.pcbway.com/helpcenter/pcb_assembly_ordering/What_files_are_requested_for_assembly_production_.html';
        panel.append(help); container.append(panel);
      }
    } catch (e) { report?.(e.message); }
    finally { pending = false; }
  }
  update();
  const timer = setInterval(update, 5000);
  return {dispose() {disposed = true; clearInterval(timer);}};
}
