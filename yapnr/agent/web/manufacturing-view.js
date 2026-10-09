const el = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
};
export function mountManufacturing(container, {project, approve, review, artifact, report} = {}) {
  let disposed = false, pending = false, key = '';
  container.classList.add('manufacturing-browser');
  async function update() {
    if (disposed || pending) return;
    pending = true;
    try {
      const response = await fetch('/yapnr/api/manufacturing/' + encodeURIComponent(project));
      if (!response.ok) throw Error('Manufacturing handoff unavailable');
      const data = await response.json();
      if (disposed || JSON.stringify(data) === key) return;
      key = JSON.stringify(data);
      container.replaceChildren(el('h2', 'Turnkey assembly'));
      container.append(el('p', 'Prepare → Review package → Vendor quote & checkout'));
      if (!data.releases.length) {
        container.append(el('p', 'No assembly package is ready for review yet. Ask the agent to prepare a JLCPCB or PCBWay assembly bundle for the selected board.'));
        const help = el('pre', 'yapnr fab build BOARD --vendor jlcpcb --assembly --parts-lock LOCK --out DIR\nyapnr workspace assembly --bundle DIR --board BOARD');
        container.append(help);
      }
      for (const release of data.releases) {
        const panel = el('section'), card = release.card;
        panel.className = 'assembly-release';
        panel.append(el('h3', card.vendor.title), el('p', release.current ? release.review.replaceAll('_', ' ') : 'Changed inputs — prepare and review a new package'));
        panel.append(el('p', `${card.board.name} · ${card.board.size_mm.join(' × ')} mm · ${card.board.layers.length} layers · quantity ${card.qty.value}`));
        panel.append(el('p', `${card.profile.title} · ${card.stackup.summary}`));
        panel.append(el('code', release.board.sha256));
        panel.append(el('p', `Native DRC: ${card.checks.drc_errors} errors, ${card.checks.drc_warnings} warnings. Fabrication: ${card.checks.summary.error} errors, ${card.checks.summary.warning} warnings.`));
        panel.append(el('p', 'Review BOM part matching, DNP/consigned items, placement orientation, polarity, assembly sides, stock and price. Vendor stock, quote and lead time are confirmed on the vendor site.'));
        for (const note of [...(card.checks.notable || []), ...(card.assembly.notes || [])]) panel.append(el('p', note));
        const files = el('ul');
        for (const file of release.downloads) {
          const row = el('li'), link = el('a', file.label);
          link.href = `/yapnr/artifact/${encodeURIComponent(project)}/${file.artifact}`;
          link.download = '';
          row.append(link, el('code', file.sha256.slice(0, 12)));
          files.append(row);
        }
        panel.append(files, el('p', release.open_verification));
        const inspect = el('button', 'Inspect package & leave feedback');
        inspect.onclick = () => artifact(release.artifact);
        panel.append(inspect);
        const confirm = el('label'), checkbox = el('input');
        checkbox.type = 'checkbox';
        confirm.append(checkbox, document.createTextNode(' I reviewed this board revision, BOM, placement/orientation and outstanding checks.'));
        panel.append(confirm);
        const accept = el('button', 'Approve package for vendor handoff');
        accept.disabled = true;
        checkbox.onchange = () => { accept.disabled = !checkbox.checked || !release.current || release.feedback.length > 0 || release.handoff_ready; };
        accept.onclick = async () => {
          accept.disabled = true;
          try { await approve(release.artifact); key = ''; await update(); }
          catch (e) { report(e.message); key = ''; await update(); }
        };
        const feedback = el('button', 'Review Feedback');
        feedback.disabled = !release.feedback.length;
        feedback.onclick = () => review(release);
        panel.append(accept, feedback);
        const vendor = el('a', release.handoff_ready ? 'Continue on ' + card.vendor.title : 'Vendor handoff awaits package approval');
        if (release.handoff_ready) {
          vendor.href = release.vendor_page;
          vendor.target = '_blank';
          vendor.rel = 'noopener noreferrer';
        } else vendor.setAttribute('aria-disabled', 'true');
        panel.append(vendor, el('p', 'You upload the files, inspect the vendor preview, obtain the quote and complete checkout. Package approval does not submit or pay for an order.'));
        container.append(panel);
      }
    } catch (e) { report?.(e.message); }
    finally { pending = false; }
  }
  update();
  const timer = setInterval(update, 5000);
  return {dispose() {disposed = true; clearInterval(timer);}};
}
