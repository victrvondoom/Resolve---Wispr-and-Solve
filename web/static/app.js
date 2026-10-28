/* RESOLVE Mission Control. Talks only to the local server (same origin). */
'use strict';
const $ = s => document.querySelector(s);
let state = null, contactId = null, reportId = null, pending = null, sessionToken = null, windowSnapshot = null, initialIntentLoaded = false, lastConfirmedActionId = null, skillProposal = null;

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function api(path, body) {
  if (body && !sessionToken) {
    const session = await fetch('/api/session');
    if (!session.ok) throw Error('Could not establish local session');
    sessionToken = (await session.json()).token;
  }
  const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Resolve-Token': sessionToken }, body: JSON.stringify(body) } : {});
  let d = {};
  try { d = await r.json(); } catch (e) { d = {}; }
  if (!r.ok) { const err = Error(d.error || r.statusText || ('HTTP ' + r.status)); err.status = r.status; err.body = d; throw err; }
  return d;
}
function errText(e) { return (e && e.status ? 'HTTP ' + e.status + ': ' : '') + (e && e.message ? e.message : String(e)); }
function chipClass(status) {
  const s = String(status || '').toUpperCase();
  if (['CONFIRMED', 'SUCCEEDED', 'ACTIVE', 'APPROVED', 'READY', 'OK', 'COMPLETED', 'DONE', 'APPLIED'].includes(s)) return 'chip-ok';
  if (['FAILED', 'WRONG_TARGET', 'CANCELLED', 'TIMED_OUT', 'LEASE_LOST', 'ERROR', 'REFUSED'].includes(s)) return 'chip-bad';
  if (['UNCERTAIN', 'NEEDS_APPROVAL', 'NEEDS_CLARIFICATION', 'DISPATCHING', 'BLOCKED', 'PENDING_APPROVAL', 'PENDING_REVIEW', 'SUPERSEDED', 'STALE', 'STALE_PLAN'].includes(s)) return 'chip-warn';
  return 'chip-muted';
}
function chip(text, cls) { return `<span class="chip ${cls || chipClass(text)}">${esc(text)}</span>`; }
function setMsg(el, text, kind) { el.textContent = text; el.classList.toggle('err', kind === 'err'); el.classList.toggle('ok', kind === 'ok'); }

/* ---------------- view router ---------------- */
const views = [...document.querySelectorAll('section.view')];
const navLinks = [...document.querySelectorAll('.sidebar a[data-view]')];
function showView(name, focus) {
  if (!views.some(v => v.dataset.view === name)) name = 'mission';
  views.forEach(v => { v.hidden = v.dataset.view !== name; });
  navLinks.forEach(a => { if (a.dataset.view === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  if (focus) { const h = document.querySelector(`#view-${name} h1`); if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); } window.scrollTo(0, 0); }
}
navLinks.forEach(a => a.addEventListener('click', ev => { ev.preventDefault(); history.replaceState(null, '', '#' + a.dataset.view); showView(a.dataset.view, true); }));
window.addEventListener('hashchange', () => showView(location.hash.slice(1), true));
showView(location.hash.slice(1) || 'mission', false);

/* ---------------- desktop simulator (existing flow) ---------------- */
function render() {
  if (!state) return;
  const m = state.missions.find(x => x.mission_id === 'mission_demo');
  if (m) $('#mission').innerHTML = `<div class="metric">${esc(m.status)}</div><p>Revision ${m.intent_revision}</p><p class="muted">${esc(m.goal)}</p><p><b>Current request:</b> ${esc(m.original_request)}</p><p class="muted">Approval required for simulated send</p>`;
  const entities = state.entities;
  renderChoices('#contacts', entities.filter(x => x.kind === 'contact'), 'contact');
  renderChoices('#reports', entities.filter(x => x.kind === 'document'), 'report');
  $('#ledger').textContent = state.events.slice(0, 40).map(e => `${e.at}  ${e.action_id || 'mission'}  ${e.state}`).join('\n') || 'No action events yet.';
}
function renderChoices(sel, items, kind) {
  $(sel).innerHTML = items.map(x => `<label class="candidate ${((kind === 'contact' ? contactId : reportId) === x.entity_id) ? 'selected' : ''}"><input type="radio" name="${kind}" value="${esc(x.entity_id)}" ${((kind === 'contact' ? contactId : reportId) === x.entity_id) ? 'checked' : ''}><span><b>${esc(x.display_name)} · ${esc(x.version || x.kind)}</b><small>${esc(x.context || '')} · ID ${esc(x.stable_id || 'unknown')} ${x.modified_at ? '· modified ' + esc(x.modified_at) : ''}</small></span></label>`).join('');
  $(sel).querySelectorAll('input').forEach(i => i.onchange = () => { if (kind === 'contact') contactId = i.value; else reportId = i.value; render(); });
}
function setHealth(text, ok) { $('#health').textContent = text; $('#healthChip').className = 'chip ' + (ok ? 'chip-ok' : 'chip-bad'); }
async function refresh() {
  state = await api('/api/state');
  if (!initialIntentLoaded) { $('#intent').value = state.missions.find(x => x.mission_id === 'mission_demo')?.original_request || ''; initialIntentLoaded = true; }
  lastConfirmedActionId = state.actions.find(x => x.operation === 'simulated_send_and_update_followup' && x.status === 'CONFIRMED')?.action_id || null;
  render();
  setHealth('healthy · ' + new Date().toLocaleTimeString(), true);
}
$('#reload').onclick = async () => { contactId = null; reportId = null; pending = null; $('#draftbox').textContent = 'Choose a contact and report to prepare a draft.'; $('#approve').disabled = true; $('#evidence').textContent = 'No verified effect yet.'; await refresh().catch(e => setHealth('offline: ' + e.message, false)); };
$('#revise').onclick = async () => { try { const m = state.missions.find(x => x.mission_id === 'mission_demo'); const x = await api('/api/mission/revise', { mission_id: m.mission_id, expected_revision: m.intent_revision, original_request: $('#intent').value }); $('#revmsg').textContent = `Saved revision ${x.intent_revision}; old actions are now stale.`; pending = null; $('#approve').disabled = true; await refresh(); } catch (e) { $('#revmsg').textContent = e.message; } };

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
if (SpeechRecognition) {
  recognition = new SpeechRecognition(); recognition.continuous = true; recognition.interimResults = true; recognition.lang = navigator.language || 'en-US';
  recognition.onstart = () => { $('#voicebadge').textContent = 'LISTENING'; $('#voiceStart').disabled = true; $('#voiceStop').disabled = false; $('#revmsg').textContent = 'Listening…'; };
  recognition.onerror = e => { $('#revmsg').textContent = 'Speech recognition error: ' + e.error; };
  recognition.onend = () => { $('#voicebadge').textContent = 'DICTATION'; $('#voiceStart').disabled = false; $('#voiceStop').disabled = true; };
  recognition.onresult = e => { let finalText = ''; let interim = ''; for (let i = e.resultIndex; i < e.results.length; i++) { if (e.results[i].isFinal) finalText += e.results[i][0].transcript; else interim += e.results[i][0].transcript; } if (finalText) { const old = $('#intent').value.trim(); $('#intent').value = (old ? old + ' ' : '') + finalText.trim(); } $('#revmsg').textContent = interim ? 'Hearing: ' + interim : 'Transcript ready for review.'; };
  $('#voiceStart').onclick = () => { try { $('#intent').value = ''; recognition.start(); } catch (e) { $('#revmsg').textContent = e.message; } };
  $('#voiceStop').onclick = () => recognition.stop();
  $('#voiceStart').title = 'Browser speech recognition; may use the browser vendor\'s service';
} else {
  $('#voiceStart').disabled = true; $('#voiceStop').disabled = true;
  $('#voiceStart').title = $('#voiceStop').title = 'This browser does not expose Web Speech recognition';
  $('#voicebadge').textContent = 'UNAVAILABLE';
  $('#revmsg').textContent = 'This browser does not expose Web Speech recognition; text entry remains available.';
}
let narrationEnabled = false;
function speak(message) { if (!narrationEnabled || !window.speechSynthesis) return; window.speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(message); u.rate = 1; window.speechSynthesis.speak(u); }
const narrationButton = document.createElement('button');
narrationButton.id = 'narration'; narrationButton.className = 'btn'; narrationButton.textContent = 'Narration off'; narrationButton.setAttribute('aria-label', 'Toggle spoken feedback');
narrationButton.disabled = !window.speechSynthesis; if (!window.speechSynthesis) narrationButton.title = 'This browser has no speech synthesis';
narrationButton.onclick = () => { narrationEnabled = !narrationEnabled; narrationButton.textContent = narrationEnabled ? 'Narration on' : 'Narration off'; if (narrationEnabled) speak('Spoken feedback is on.'); else window.speechSynthesis.cancel(); };
$('#voiceStop').after(narrationButton);
function handleVoiceCommand(phrase) {
  const command = phrase.trim().toLowerCase().replace(/[.!?]+$/, '');
  if (command === 'resolve stop' || command === 'emergency stop') { $('#estop').click(); return true; }
  if (command === 'resolve resume') { $('#resume').click(); return true; }
  if (command === 'resolve discover') { $('#discover').click(); return true; }
  if (command === 'resolve submit') { $('#revise').click(); return true; }
  if (command === 'resolve prepare') { $('#draft').click(); return true; }
  if (command === 'resolve approve simulated send') { if (pending) $('#approve').click(); else $('#revmsg').textContent = 'No action is waiting for approval.'; return true; }
  if (command === 'resolve choose northwind alex') { const matches = (state?.entities || []).filter(e => e.kind === 'contact' && e.display_name.toLowerCase().includes('alex') && e.context === 'Northwind account'); if (matches.length === 1) { contactId = matches[0].entity_id; render(); $('#revmsg').textContent = 'Northwind Alex selected.'; } else $('#revmsg').textContent = 'Contact remains ambiguous.'; return true; }
  if (command === 'resolve choose latest report') { const context = state?.missions?.find(m => m.mission_id === 'mission_demo')?.constraints?.client; const rows = (state?.entities || []).filter(e => e.kind === 'document' && e.context === context && e.modified_at).sort((a, b) => b.modified_at.localeCompare(a.modified_at)); if (rows.length && (!rows[1] || rows[0].modified_at !== rows[1].modified_at)) { reportId = rows[0].entity_id; render(); $('#revmsg').textContent = 'Latest report selected.'; } else $('#revmsg').textContent = 'Report version remains ambiguous.'; return true; }
  return false;
}
if (recognition) { const originalResult = recognition.onresult; recognition.onresult = e => { let finalText = ''; for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) finalText += e.results[i][0].transcript; if (finalText && handleVoiceCommand(finalText)) return; originalResult(e); }; $('#voicebadge').textContent = 'DICTATION READY'; }

$('#draft').onclick = async () => { try { if (!contactId || !reportId) throw Error('Select one contact and one report first.'); pending = await api('/api/action/draft', { contact_id: contactId, report_id: reportId }); $('#draftbox').innerHTML = `<p><b>${esc(pending.contact.display_name)}</b> (${esc(pending.contact.stable_id)})</p><p>Attachment: <b>${esc(pending.report.display_name)}</b> ${esc(pending.report.version)}</p><p>Action digest: <code>${esc(pending.digest)}</code></p><div class="notice">This exact operation is waiting for approval. Nothing has been sent.</div>`; $('#approve').disabled = false; $('#actionmsg').textContent = 'Approval is bound to this digest and mission revision.'; await refresh(); } catch (e) { $('#actionmsg').textContent = e.message; } };
$('#approve').onclick = async () => { if (!pending) return; $('#approve').disabled = true; try { const r = await api('/api/action/approve-send', { action_id: pending.action.action_id, digest: pending.digest }); const ev = r.verification.evidence; $('#evidence').innerHTML = `<div class="metric ${r.verification.status === 'CONFIRMED' ? 'success' : ''}">${esc(r.verification.status)}</div><p>Local simulator independently checked one matching outbox message and the follow-up record.</p><p>Recipient ID: <code>${esc(ev.recipient_id)}</code></p><p>Attachment ID: <code>${esc(ev.attachment_id)}</code></p><p>Message ID: <code>${esc(ev.message_id)}</code></p><p>Follow-up: <code>${esc(ev.followup_id)}</code> · ${esc(ev.followup_status)} · linked message ${esc(ev.followup_last_message_id)}</p><p class="muted">Evidence source: ${esc(ev.source)} · ${esc(ev.sent_at)}</p>`; pending = null; await refresh(); } catch (e) { $('#actionmsg').textContent = e.message; $('#approve').disabled = false; } };

/* Windows native (guarded Win32 input) */
$('#windows').onclick = async () => { try { const r = await api('/api/windows'); windowSnapshot = r.windows.find(w => w.foreground)?.snapshot_id || null; $('#hwnd').innerHTML = '<option value="">Choose a visible window…</option>' + r.windows.map(w => `<option value="${w.hwnd}" ${w.foreground ? 'selected' : ''}>${esc(w.title)} · PID ${w.pid}${w.foreground ? ' · foreground' : ''}</option>`).join(''); $('#winlog').textContent = JSON.stringify(r, null, 2); } catch (e) { $('#winlog').textContent = 'Observation unavailable: ' + e.message; } };
async function winAction(kind) { try { const hwnd = $('#hwnd').value; if (!hwnd) throw Error('Inspect and select a window first.'); if (!windowSnapshot) throw Error('Inspect windows to obtain a fresh snapshot.'); const mission = state.missions.find(x => x.mission_id === 'mission_demo'); const payload = { hwnd: Number(hwnd), snapshot_id: windowSnapshot, mission_id: mission.mission_id, intent_revision: mission.intent_revision }; if (kind === 'move' || kind === 'click') { if (!$('#px').value || !$('#py').value) throw Error('Enter screen coordinates within the selected window.'); payload.x = Number($('#px').value); payload.y = Number($('#py').value); } if (kind === 'type') { payload.text = $('#typevalue').value; if (!payload.text) throw Error('Enter text to type.'); } const r = await api('/api/windows/' + kind, payload); windowSnapshot = null; $('#winlog').textContent = JSON.stringify(r, null, 2) + '\nRefresh window observation before the next input.'; await refresh(); } catch (e) { windowSnapshot = null; $('#winlog').textContent = 'Action refused: ' + e.message + ' Refresh window observation before retrying.'; } }
$('#move').onclick = () => winAction('move'); $('#click').onclick = () => winAction('click'); $('#type').onclick = () => winAction('type');
api('/api/health').then(() => refresh()).catch(e => setHealth('offline: ' + e.message, false));

$('#discover').onclick = async () => { try { $('#agentlog').textContent = 'Running contact and report specialists…'; const r = await api('/api/agents/discover', { mission_id: 'mission_demo' }); $('#agentlog').textContent = JSON.stringify(r, null, 2); await refresh(); } catch (e) { $('#agentlog').textContent = 'Agent run failed: ' + e.message; } };
$('#estop').onclick = async () => { window.speechSynthesis?.cancel(); try { const r = await api('/api/emergency-stop', {}); windowSnapshot = null; $('#agentlog').textContent = JSON.stringify(r, null, 2); setHealth('STOPPED · cursor lease revoked', false); } catch (e) { $('#agentlog').textContent = e.message; } };
$('#resume').onclick = async () => { try { const r = await api('/api/resume', {}); $('#agentlog').textContent = JSON.stringify(r, null, 2); setHealth('ready · ' + new Date().toLocaleTimeString(), true); } catch (e) { $('#agentlog').textContent = e.message; } };

api('/api/benchmark').then(r => { $('#benchlog').textContent = `Seed ${r.seeds.join(', ')} · ${r.sample_size} cases · ${r.method}\n\n` + Object.entries(r.modes).map(([name, m]) => `${name}: correct ${Math.round(m.objective_accuracy * m.n)}/${m.n}, wrong target ${Math.round(m.wrong_target_rate * m.n)}/${m.n}, false success ${Math.round(m.false_success_rate * m.n)}/${m.n}, duplicate effects ${Math.round(m.duplicate_effect_rate * m.n)}/${m.n}`).join('\n'); }).catch(e => { $('#benchlog').textContent = 'No recorded benchmark available: ' + e.message; });
api('/api/workspaces').then(r => { $('#workspaceCount').textContent = `${r.total_concepts} planned concepts · ${r.workspaces.length} workspaces`; $('#workspace').innerHTML = '<option value="">Choose a workspace…</option>' + r.workspaces.map(x => `<option value="${esc(x.name)}">${esc(x.name)} (${x.planned_concepts})</option>`).join(''); }).catch(e => { $('#workspacelog').textContent = e.message; });
$('#workspace').onchange = async () => { const name = $('#workspace').value; if (!name) { $('#workspacelog').textContent = 'Choose a workspace to inspect planned concepts.'; return; } try { const r = await api('/api/workspaces?workspace=' + encodeURIComponent(name)); $('#workspacelog').textContent = r.concepts.map(x => `${x.name} · ${x.implementation_status} · ${x.source_reference}`).join('\n'); } catch (e) { $('#workspacelog').textContent = e.message; } };

const oldRevise = $('#revise').onclick; $('#revise').onclick = async event => { await oldRevise(event); const mission = state?.missions?.find(x => x.mission_id === 'mission_demo'); if (mission && $('#revmsg').textContent.startsWith('Saved revision')) speak('Mission revision ' + mission.intent_revision + '. Status ' + mission.status.toLowerCase().replaceAll('_', ' ') + '.'); };
const oldDraft = $('#draft').onclick; $('#draft').onclick = async event => { pending = null; $('#approve').disabled = true; await oldDraft(event); if (pending) speak('Draft ready for ' + pending.contact.display_name + ', ' + pending.contact.context + ', with report ' + pending.report.version + '. Say Resolve approve simulated send to approve this local simulator action.'); };
const oldApprove = $('#approve').onclick; $('#approve').onclick = async event => { await oldApprove(event); const status = $('#evidence .metric')?.textContent; if (status) speak('Local simulator effect ' + status.toLowerCase() + '.'); };
const oldDiscover = $('#discover').onclick; $('#discover').onclick = async event => { await oldDiscover(event); if ($('#agentlog').textContent.includes('SUCCEEDED')) { const candidates = (state?.entities || []).filter(e => e.kind === 'contact' && e.display_name.toLowerCase().includes('alex')); speak('The contact and report agents finished. I found ' + candidates.length + ' Alex contacts: ' + candidates.map(e => e.context).join(' and ') + '. Say Resolve choose Northwind Alex to clarify the recipient.'); } };
['#contacts', '#reports'].forEach(selector => $(selector).addEventListener('change', () => { pending = null; $('#approve').disabled = true; $('#draftbox').textContent = 'Selection changed. Prepare a new draft before approval.'; }));

const captureButton = document.createElement('button'); captureButton.id = 'capture'; captureButton.className = 'btn'; captureButton.textContent = 'Capture selected window'; $('#windows').after(captureButton);
const frame = document.createElement('img'); frame.id = 'frame'; frame.alt = 'Actual captured pixels from the selected foreground window'; frame.hidden = true; $('#winlog').after(frame); let frameUrl = null;
captureButton.onclick = async () => { try { const hwnd = $('#hwnd').value; if (!hwnd || !windowSnapshot) throw Error('Inspect and select a fresh foreground window first.'); const mission = state.missions.find(x => x.mission_id === 'mission_demo'); const capture = await api('/api/windows/capture', { mission_id: mission.mission_id, intent_revision: mission.intent_revision, hwnd: Number(hwnd), snapshot_id: windowSnapshot }); const response = await fetch('/api/evidence/' + encodeURIComponent(capture.image_name), { headers: { 'X-Resolve-Token': sessionToken } }); if (!response.ok) throw Error('Could not load captured evidence'); if (frameUrl) URL.revokeObjectURL(frameUrl); frameUrl = URL.createObjectURL(await response.blob()); frame.src = frameUrl; frame.hidden = false; $('#winlog').textContent = JSON.stringify(capture, null, 2); } catch (e) { $('#winlog').textContent = 'Capture unavailable: ' + e.message; } };
const uiaButton = document.createElement('button'); uiaButton.id = 'uiaInspect'; uiaButton.className = 'btn'; uiaButton.textContent = 'Inspect UI elements'; captureButton.after(uiaButton);
uiaButton.onclick = async () => { try { const hwnd = $('#hwnd').value; if (!hwnd || !windowSnapshot) throw Error('Inspect and select a fresh foreground window first.'); const mission = state.missions.find(x => x.mission_id === 'mission_demo'); const tree = await api('/api/uia/inspect', { mission_id: mission.mission_id, intent_revision: mission.intent_revision, hwnd: Number(hwnd), snapshot_id: windowSnapshot }); $('#winlog').textContent = JSON.stringify(tree, null, 2); } catch (e) { $('#winlog').textContent = 'UI Automation inspection unavailable: ' + e.message; } };
api('/api/uia/availability').then(r => { if (!uiaButton.disabled) uiaButton.title = r.available ? 'Read-only UI Automation inspection available' : 'Optional UI Automation: ' + r.reason; }).catch(() => {});

async function loadSkills() { const listing = await api('/api/skills'); const active = listing.skills.filter(x => x.status === 'ACTIVE'); $('#skillList').innerHTML = '<option value="">Choose active skill…</option>' + active.map(x => `<option value="${esc(x.skill_id)}">${esc(x.spec.name)} · v${x.version}</option>`).join(''); const proposal = listing.skills.find(x => x.status === 'PENDING_APPROVAL'); if (proposal) { skillProposal = { skill: proposal.spec, review_digest: proposal.review_digest }; $('#skillActivate').disabled = false; $('#skilllog').textContent = 'Review pending skill and digest before activation:\n' + JSON.stringify(skillProposal, null, 2); } }
$('#skillPropose').onclick = async () => { try { if (!lastConfirmedActionId) throw Error('Complete and verify a local simulator action first.'); skillProposal = await api('/api/skills/propose', { action_id: lastConfirmedActionId }); $('#skillActivate').disabled = false; $('#skilllog').textContent = 'Review this exact skill and digest before activation:\n' + JSON.stringify(skillProposal, null, 2); } catch (e) { $('#skilllog').textContent = 'Skill proposal refused: ' + e.message; } };
$('#skillActivate').onclick = async () => { try { if (!skillProposal) throw Error('No skill proposal is ready.'); const r = await api('/api/skills/activate', { skill_id: skillProposal.skill.skill_id, version: skillProposal.skill.version, review_digest: skillProposal.review_digest }); $('#skilllog').textContent = 'Activated reviewed skill:\n' + JSON.stringify(r, null, 2); skillProposal = null; $('#skillActivate').disabled = true; await loadSkills(); } catch (e) { $('#skilllog').textContent = 'Skill activation refused: ' + e.message; } };
$('#skillReplay').onclick = async () => { try { const skillId = $('#skillList').value; if (!skillId) throw Error('Select an active skill.'); if (!contactId) throw Error('Select the intended contact first.'); const mission = state.missions.find(x => x.mission_id === 'mission_demo'); const r = await api('/api/skills/replay', { skill_id: skillId, mission_id: mission.mission_id, expected_revision: mission.intent_revision, inputs: { context: mission.constraints.client, contact_id: contactId } }); pending = r.draft; contactId = r.plan.contact.entity_id; reportId = r.plan.report.entity_id; render(); $('#approve').disabled = false; $('#draftbox').innerHTML = `<p><b>${esc(pending.contact.display_name)}</b> · ${esc(pending.contact.stable_id)}</p><p>Current attachment: ${esc(pending.report.display_name)} · ${esc(pending.report.version)}</p><p>Action digest: <code>${esc(pending.digest)}</code></p><div class="notice">Skill replay prepared a new local draft. Approval is still required.</div>`; $('#skilllog').textContent = JSON.stringify(r.plan, null, 2); await refresh(); } catch (e) { $('#skilllog').textContent = 'Skill replay refused: ' + e.message; } };
loadSkills().catch(e => { $('#skilllog').textContent = 'Skill registry unavailable: ' + e.message; });

/* ---------------- runtime status strip ---------------- */
const WIN_CONTROLS = ['#windows', '#hwnd', '#px', '#py', '#move', '#click', '#typevalue', '#type', '#capture', '#uiaInspect'];
function disableWindows(reason) {
  WIN_CONTROLS.forEach(s => { const el = $(s); if (el) { el.disabled = true; el.title = 'Unavailable: ' + reason; } });
  $('#winChip').textContent = 'UNAVAILABLE ON THIS HOST'; $('#winChip').className = 'chip chip-warn';
  setMsg($('#winAvail'), 'Native Windows control is unavailable here: ' + reason + '. WINDOWS_NATIVE_TESTS_NOT_RUN on this host — run scripts/smoke-windows.ps1 in an interactive Windows session.', 'err');
}
let runtimeStatus = null;
api('/api/web/status').then(s => {
  runtimeStatus = s;
  const wn = s.windows_native || {}, sp = s.speech || {};
  $('#statusStrip').innerHTML = [
    chip('Playwright: ' + (s.playwright ? 'available' : 'unavailable'), s.playwright ? 'chip-ok' : 'chip-bad'),
    `<span class="chip ${wn.available ? 'chip-ok' : 'chip-warn'}" title="${esc(wn.reason || '')}">Windows native: ${wn.available ? 'available' : 'unavailable'}${wn.reason ? ' — ' + esc(wn.reason) : ''}</span>`,
    `<span class="chip chip-muted" title="${esc(sp.note || '')}">Speech: ${esc(sp.mode || 'unknown')}${sp.note ? ' — ' + esc(sp.note) : ''}</span>`,
    chip('Provider: ' + (s.provider || 'unknown'), 'chip-muted'),
    chip(s.test_mode ? 'Test mode: on (fixtures)' : 'Test mode: off', s.test_mode ? 'chip-warn' : 'chip-muted'),
    chip('Outbox: SIMULATED DELIVERY', 'chip-warn')
  ].join('');
  if (wn.available === false) disableWindows(wn.reason || 'not a Windows interactive session');
  else setMsg($('#winAvail'), 'Server reports native Windows control is available.', 'ok');
  if (sp.note) $('#utterMicChip').title = sp.note;
}).catch(e => {
  $('#statusStrip').innerHTML = `<span class="chip chip-bad">Runtime status unavailable — ${esc(errText(e))}</span>` + chip('Outbox: SIMULATED DELIVERY', 'chip-warn');
  setMsg($('#winAvail'), 'Runtime status unavailable (' + errText(e) + '); Win32 requests will report their own errors.', 'err');
});

/* The first world-dependent request initialises the fixture apps on the server; issue it alone,
   then let the other world-dependent requests follow, so first-load requests never race each other. */
const worldGate = api('/api/web/agents').then(r => ({ ok: r }), e => ({ error: e }));

/* ---------------- browser mission (signature workflow) ---------------- */
const WM = (function () {
  const $ = id => document.getElementById(id);
  let mid = null, plan = null, es = null, current = null;
  const listeners = [];
  try { mid = localStorage.getItem('wm_mid'); } catch (e) {}
  function setMid(id) { mid = id; try { if (id) localStorage.setItem('wm_mid', id); else localStorage.removeItem('wm_mid'); } catch (e) {} listeners.forEach(f => f(mid)); markSelected(); }
  function renderActions(m) {
    const acts = m.actions_detail || [];
    $('wmactions').innerHTML = acts.map(a => {
      const v = a.verification || {};
      const ev = v.evidence ? Object.entries(v.evidence).slice(0, 4).map(([k, x]) => `${k}: ${typeof x === 'object' ? JSON.stringify(x) : x}`).join(' · ') : '';
      return `<div class="action"><span class="op">${esc(a.operation)}</span><span class="chips">${chip(a.status)}${v.status ? chip('verify: ' + v.status, chipClass(v.status)) : ''}</span><span class="meta">${esc(a.action_id)}${ev ? ' · ' + esc(ev) : ''}</span></div>`;
    }).join('');
  }
  function renderUtterances(m) {
    const u = m.utterances || [];
    $('utterHistory').innerHTML = u.map(x => `<li>${chip('r' + (x.revision ?? '?'), 'chip-muted')}${chip(x.kind || 'UTTERANCE', 'chip-muted')}<span>${esc(x.text)}</span></li>`).join('');
  }
  const show = m => {
    current = m;
    $('wmstate').textContent = JSON.stringify({ status: m.status, revision: m.intent_revision, binding: m.binding, report: m.report && { id: m.report.id, version: m.report.version, sha256: (m.report.sha256 || '').slice(0, 12) }, actions: (m.actions_detail || []).map(a => ({ op: a.operation, status: a.status, verification: a.verification && a.verification.status })) }, null, 2);
    $('wmcands').innerHTML = '';
    if (m.status === 'NEEDS_CLARIFICATION') {
      (m.candidates || []).forEach(c => { const b = document.createElement('button'); b.className = 'btn'; b.textContent = 'Choose ' + c.id + ' · ' + c.email + ' (' + c.org + '/' + c.dept + ')'; b.onclick = () => step(() => api('/api/web/missions/' + mid + '/bind', { contact_id: c.id, revision: m.intent_revision })); $('wmcands').appendChild(b); });
      $('wmmsg').textContent = m.ambiguity_reason || 'Clarification needed.';
    }
    const acts = m.actions_detail || [], task = acts.find(a => a.operation === 'crm_create_task'), send = acts.find(a => a.operation === 'outbox_send');
    $('wmplan').disabled = !m.binding || acts.length > 0 && !send || (send && send.status === 'CONFIRMED');
    $('wmapprove').disabled = !(send && send.status === 'NEEDS_APPROVAL');
    $('wmtask').disabled = !(task && task.status === 'BOUND');
    $('wmsend').disabled = !(send && send.status === 'AUTHORIZED');
    $('wmfault').disabled = !mid;
    renderActions(m); renderUtterances(m);
  };
  async function refresh() { if (!mid) return; const m = await api('/api/web/missions/' + mid); show(m); return m; }
  async function step(f) {
    try { $('wmmsg').textContent = 'Working…'; const r = await f(); await refresh(); $('wmmsg').textContent = r && r.status ? 'Result: ' + r.status : 'Done'; }
    catch (e) { $('wmmsg').textContent = 'Refused: ' + e.message; await refresh().catch(() => {}); }
    loadList();
  }
  const ACTION_STATES = ['proposed', 'bound', 'needs_approval', 'authorized', 'ready', 'dispatching', 'dispatched', 'observing', 'confirmed', 'failed', 'uncertain', 'wrong_target', 'superseded', 'cancelled', 'reconciling'];
  const TASK_STATES = ['pending', 'ready', 'running', 'succeeded', 'failed', 'cancelled', 'blocked', 'timed_out', 'lease_lost', 'superseded'];
  function connect() {
    if (es) es.close();
    es = new EventSource('/api/stream?after=0&max_s=3600');
    const sc = document.getElementById('streamChip');
    es.onopen = () => { $('wmstream').textContent = ''; sc.textContent = 'SSE · connected'; sc.className = 'chip chip-ok'; };
    es.onerror = () => { $('wmstream').textContent += '\n[stream disconnected]'; sc.textContent = 'SSE · reconnecting'; sc.className = 'chip chip-warn'; };
    es.onmessage = () => {};
    const append = line => { const el = $('wmstream'); const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 8; el.textContent += line + '\n'; if (stick) el.scrollTop = el.scrollHeight; };
    ACTION_STATES.forEach(k => es.addEventListener('action.' + k, ev => { const d = JSON.parse(ev.data); append('#' + d.seq + ' ' + d.state + ' ' + d.action_id); }));
    TASK_STATES.forEach(k => es.addEventListener('task.' + k, ev => { const d = JSON.parse(ev.data); append('#' + d.seq + ' task ' + d.status + ' ' + d.task_id + (d.mission_id ? ' (' + d.mission_id + ')' : '')); }));
  }
  function markSelected() { document.querySelectorAll('#missionList button[data-mid]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mid === mid))); }
  async function loadList() {
    const list = $('missionList');
    try {
      const r = await api('/api/web/missions');
      const ms = (r.missions || []).slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
      if (!ms.length) { list.innerHTML = '<li class="muted">No sessions yet. Start a mission.</li>'; return; }
      list.innerHTML = ms.map(m => `<li><button class="btn" data-mid="${esc(m.mission_id)}" aria-pressed="false"><span>${esc((m.goal || '').slice(0, 80) || 'Mission')}</span><span>${chip(m.status)} <span class="muted small">rev ${esc(m.intent_revision)}</span></span><span class="sid">${esc(m.mission_id)} · ${esc(m.created_at)}</span></button></li>`).join('');
      list.querySelectorAll('button[data-mid]').forEach(b => b.onclick = async () => { setMid(b.dataset.mid); plan = null; try { await refresh(); $('wmmsg').textContent = 'Loaded session ' + b.dataset.mid + '.'; } catch (e) { $('wmmsg').textContent = 'Could not load session: ' + errText(e); } });
      markSelected();
    } catch (e) { list.innerHTML = `<li class="msg err">Session list unavailable — ${esc(errText(e))}</li>`; }
  }
  $('missionsReload').onclick = loadList;
  $('wmstart').onclick = () => step(async () => { await api('/api/web/world', {}); const m = await api('/api/web/missions', { command: $('wmcmd').value }); setMid(m.mission_id); plan = null; connect(); return m; });
  $('wmplan').onclick = () => step(async () => { plan = await api('/api/web/missions/' + mid + '/plan', {}); $('wmmsg').textContent = 'Approval digest ' + plan.approval_digest.slice(0, 16) + '…'; return plan; });
  $('wmapprove').onclick = () => step(async () => { const m = await api('/api/web/missions/' + mid); const send = m.actions_detail.find(a => a.operation === 'outbox_send'); const d = plan && plan.send_action === send.action_id ? plan.approval_digest : null; if (!d) throw Error('plan again in this session to see the exact digest'); return api('/api/web/actions/' + send.action_id + '/approve', { digest: d }); });
  $('wmtask').onclick = () => step(async () => { const m = await api('/api/web/missions/' + mid); return api('/api/web/actions/' + m.actions_detail.find(a => a.operation === 'crm_create_task').action_id + '/run', {}); });
  $('wmsend').onclick = () => step(async () => { const m = await api('/api/web/missions/' + mid); return api('/api/web/actions/' + m.actions_detail.find(a => a.operation === 'outbox_send').action_id + '/run', {}); });
  $('wmfault').onclick = () => step(() => api('/api/web/faults', { app: 'outbox', name: 'drop_ack' }));
  connect();
  loadList();
  if (mid) { worldGate.then(() => api('/api/web/missions/' + mid)).then(m => { show(m); listeners.forEach(f => f(mid)); }).catch(() => { setMid(null); }); }
  return { get mid() { return mid; }, get current() { return current; }, show, refresh, loadList, onChange(f) { listeners.push(f); f(mid); } };
})();

/* ---------------- utterances (voice or typed) ---------------- */
(function () {
  const box = $('#utterText'), mic = $('#utterMic'), submit = $('#utterSubmit'), hint = $('#utterHint'), out = $('#utterResult'), micChip = $('#utterMicChip');
  WM.onChange(mid => { submit.disabled = !mid; submit.title = mid ? 'Send this text to mission ' + mid : 'Select or start a mission first'; });
  let rec = null, listening = false;
  if (SpeechRecognition) {
    micChip.textContent = 'Browser speech recognition; may use the browser vendor\'s service'; micChip.className = 'chip chip-muted';
    mic.disabled = false; mic.title = 'Browser speech recognition; may use the browser vendor\'s service. The transcript is only submitted when you press Submit.';
    rec = new SpeechRecognition(); rec.continuous = true; rec.interimResults = true; rec.lang = navigator.language || 'en-US';
    rec.onstart = () => { listening = true; mic.textContent = 'Stop microphone'; hint.textContent = 'Listening… (nothing is submitted automatically)'; };
    rec.onend = () => { listening = false; mic.textContent = 'Start microphone'; hint.textContent = box.value ? 'Review the transcript, then press Submit.' : ''; };
    rec.onerror = e => { hint.textContent = 'Speech recognition error: ' + e.error; };
    rec.onresult = e => { let fin = '', interim = ''; for (let i = e.resultIndex; i < e.results.length; i++) { if (e.results[i].isFinal) fin += e.results[i][0].transcript; else interim += e.results[i][0].transcript; } if (fin) box.value = ((box.value.trim() ? box.value.trim() + ' ' : '') + fin.trim()); hint.textContent = interim ? 'Hearing (partial, not submitted): ' + interim : 'Transcript ready for review.'; };
    mic.onclick = () => { try { if (listening) rec.stop(); else rec.start(); } catch (e) { hint.textContent = e.message; } };
  } else {
    micChip.textContent = 'Speech unavailable in this browser'; micChip.className = 'chip chip-warn';
    mic.disabled = true; mic.title = 'This browser does not expose the Web Speech API; type the utterance instead.';
  }
  submit.onclick = async () => {
    const mid = WM.mid, text = box.value.trim();
    if (!mid) { setMsg(out, 'Select or start a mission first.', 'err'); return; }
    if (!text) { setMsg(out, 'Type or dictate an utterance first.', 'err'); return; }
    if (listening && rec) rec.stop();
    submit.disabled = true; setMsg(out, 'Classifying…');
    try {
      const r = await api('/api/web/missions/' + encodeURIComponent(mid) + '/utterances', { text });
      const c = r.classification || {};
      const extra = Object.entries(c).filter(([k]) => k !== 'kind').map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
      out.innerHTML = `Classified as ${chip(c.kind || 'UNKNOWN', 'chip-muted')} <span class="muted small">${esc(extra)}</span>`;
      out.classList.remove('err');
      if (r.mission) WM.show({ ...r.mission, actions_detail: r.mission.actions_detail || (WM.current && WM.current.actions_detail) || [] });
      await WM.refresh().catch(() => {});
      WM.loadList();
      box.value = '';
    } catch (e) { setMsg(out, 'Utterance not accepted — ' + errText(e), 'err'); }
    finally { submit.disabled = !WM.mid; }
  };
})();

/* ---------------- agents ---------------- */
(function () {
  const btn = $('#supervise'), tl = $('#taskTimeline'), forEl = $('#superviseFor');
  WM.onChange(mid => { btn.disabled = !mid; btn.title = mid ? 'Run the supervisor on ' + mid : 'Select or start a browser mission first'; forEl.textContent = mid ? 'Selected mission: ' + mid : 'No mission selected. Start or select one in Browser mission.'; });
  function fmt(s) { return s < 1 ? (s * 1000).toFixed(0) + ' ms' : s.toFixed(2) + ' s'; }
  function renderTasks(tasks) {
    if (!tasks.length) { tl.innerHTML = '<p class="muted">The supervisor returned no tasks.</p>'; return; }
    const num = v => (v === null || v === undefined || v === '' ? NaN : Number(v)); const starts = tasks.map(t => num(t.started_at)).filter(x => Number.isFinite(x) && x > 0), ends = tasks.map(t => num(t.finished_at)).filter(x => Number.isFinite(x) && x > 0);
    const t0 = starts.length ? Math.min(...starts) : 0, t1 = Math.max(t0, ...(ends.length ? ends : starts)), span = Math.max(t1 - t0, 1e-6);
    const rows = tasks.slice().sort((a, b) => (num(a.started_at) || Infinity) - (num(b.started_at) || Infinity)).map(t => {
      const s = num(t.started_at), f = num(t.finished_at), hasS = Number.isFinite(s) && s > 0, hasF = hasS && Number.isFinite(f) && f > 0;
      const left = hasS ? (s - t0) / span * 100 : 0, width = hasS && hasF ? Math.max((f - s) / span * 100, 0.5) : 0;
      const cls = chipClass(t.status) === 'chip-bad' ? 'bad' : chipClass(t.status) === 'chip-warn' ? 'warn' : '';
      const dur = hasS && hasF ? fmt(f - s) : hasS ? 'running' : 'not started';
      const deps = t.dependencies || t.depends_on;
      const detail = t.error ? 'error: ' + t.error : t.result != null ? 'result: ' + JSON.stringify(t.result).slice(0, 220) : '';
      return `<div class="tl-row"><span class="tl-name">${esc(t.agent_id || t.task_id)}</span><div class="tl-track" role="img" aria-label="${esc((t.agent_id || '') + ' ' + t.status + ' ' + dur + (hasS ? ', started ' + fmt(s - t0) + ' after the first task' : ''))}">${width ? `<span class="tl-bar ${cls}" style="left:${left}%;width:${width}%"></span>` : ''}</div><span class="tl-meta">${chip(t.status)} ${esc(dur)}</span>` +
        `<div class="tl-detail">${esc(t.task_id)}${hasS ? ' · +' + esc(fmt(s - t0)) : ''}${deps && deps.length ? ' · after ' + esc([].concat(deps).join(', ')) : ''}${detail ? ' · ' + esc(detail) : ''}</div></div>`;
    }).join('');
    tl.innerHTML = rows + `<div class="tl-axis" aria-hidden="true"><span>0</span><span>${esc(fmt(span))}</span></div>`;
  }
  btn.onclick = async () => {
    const mid = WM.mid; if (!mid) return;
    btn.disabled = true; tl.innerHTML = '<p class="muted">Running supervisor…</p>';
    $('#superviseSummary').textContent = '';
    try {
      const r = await api('/api/web/missions/' + encodeURIComponent(mid) + '/supervise', { phase: $('#supervisePhase').value });
      renderTasks(r.tasks || []);
      const bits = [];
      if ('planned' in r) bits.push('Planned: ' + (r.planned ? 'yes' : 'no'));
      if (r.reason) bits.push('Reason: ' + r.reason);
      if (r.retried && r.retried.length) bits.push('Retried read-only tasks: ' + r.retried.map(x => x.original + ' → ' + x.status).join(', '));
      if (r.safety) bits.push('Safety review: ' + JSON.stringify(r.safety).slice(0, 300));
      if (r.verification) bits.push('Verification: ' + JSON.stringify(r.verification).slice(0, 300));
      $('#superviseSummary').textContent = bits.join(' · ');
      WM.refresh().catch(() => {});
    }
    catch (e) { tl.innerHTML = `<p class="msg err">Supervisor unavailable — ${esc(errText(e))}</p>`; }
    finally { btn.disabled = !WM.mid; }
  };
  worldGate.then(g => { if (g.error) throw g.error; return g.ok; }).then(r => {
    const a = r.agents || [];
    $('#agentRoster').innerHTML = a.length ? a.map(x => `<li><div class="row" style="margin:0;justify-content:space-between"><b>${esc(x.role)}</b>${x.read_only ? chip('read-only', 'chip-ok') : chip('can write', 'chip-warn')}</div><div class="tools">${(x.tools || []).map(t => `<code>${esc(t)}</code>`).join('') || '<span class="muted small">no tools</span>'}</div></li>`).join('') : '<li class="muted">No agents registered.</li>';
  }).catch(e => { $('#agentRoster').innerHTML = `<li class="msg err">Agent roster unavailable — ${esc(errText(e))}</li>`; });
})();

/* ---------------- browser skills ---------------- */
(function () {
  const list = $('#webSkills'), log = $('#webSkillLog');
  const params = () => ({ contact_id: $('#skillContact').value.trim(), title: $('#skillTitle').value.trim() });
  const isApproved = s => ['ACTIVE', 'APPROVED'].includes(String(s.status || '').toUpperCase());
  async function load() {
    try {
      const r = await api('/api/web/skills'); const skills = r.skills || [];
      if (!skills.length) { list.innerHTML = '<li class="muted">No recorded skills yet.</li>'; return; }
      list.innerHTML = skills.map((s, i) => {
        const id = s.skill_id || s.id, name = s.name || s.title || (s.spec && s.spec.name) || id, approved = isApproved(s), known = s.status != null, pendingReview = !known || /PENDING/i.test(String(s.status));
        return `<li><span><b>${esc(name)}</b> <span class="muted small">${esc(id)} · v${esc(s.version)}</span> ${s.status ? chip(s.status) : ''}</span><span class="row" style="margin:0">` +
          `<button class="btn btn-small" data-act="approve" data-i="${i}" ${pendingReview ? `title="Approve exactly version ${esc(s.version)}"` : `disabled title="Only versions pending review can be approved (this one is ${esc(s.status)})"`}>Approve v${esc(s.version)}</button>` +
          `<button class="btn btn-small" data-act="replay" data-i="${i}" ${known && !approved ? 'disabled title="Approve this version before replaying it"' : 'title="Replay with the contact ID and title from the form"'}>Replay</button></span></li>`;
      }).join('');
      list.querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
        const s = skills[Number(b.dataset.i)], id = s.skill_id || s.id;
        try {
          let r;
          if (b.dataset.act === 'approve') r = await api('/api/web/skills/' + encodeURIComponent(id) + '/approve', { version: s.version });
          else { const body = { params: params() }; if ($('#skillFault').checked) body.fault = 'layout_shift'; r = await api('/api/web/skills/' + encodeURIComponent(id) + '/replay', body); }
          log.textContent = (b.dataset.act === 'approve' ? 'Approved:\n' : 'Replay result:\n') + JSON.stringify(r, null, 2);
        } catch (e) { log.textContent = (b.dataset.act === 'approve' ? 'Approval refused — ' : 'Replay refused — ') + errText(e) + (e.body ? '\n' + JSON.stringify(e.body, null, 2) : ''); }
        load();
      });
    } catch (e) { list.innerHTML = `<li class="msg err">Skill list unavailable — ${esc(errText(e))}</li>`; }
  }
  $('#webSkillsReload').onclick = load;
  $('#skillRecord').onclick = async () => {
    const p = params(); if (!p.contact_id || !p.title) { log.textContent = 'Enter a contact ID and a title.'; return; }
    log.textContent = 'Recording through the browser…';
    try { const r = await api('/api/web/skills/record', p); log.textContent = 'Recorded (needs approval before replay):\n' + JSON.stringify(r, null, 2); }
    catch (e) { log.textContent = 'Recording refused — ' + errText(e); }
    load();
  };
  worldGate.then(load);
})();

/* ---------------- files sandbox ---------------- */
(function () {
  const msg = $('#filesMsg'), view = $('#filesView'), log = $('#filesLog'), apply = $('#filesApply'), undo = $('#filesUndo');
  let planId = null, opId = null;
  function table(rows) {
    const objs = rows.filter(x => x && typeof x === 'object');
    if (!objs.length) return `<p class="small">${rows.slice(0, 50).map(esc).join(', ')}</p>`;
    const cols = [...new Set(objs.flatMap(o => Object.keys(o)))].slice(0, 6);
    const cell = v => typeof v === 'object' && v !== null ? JSON.stringify(v) : v;
    return `<div class="table-scroll"><table><thead><tr>${cols.map(c => `<th scope="col">${esc(c)}</th>`).join('')}</tr></thead><tbody>${objs.slice(0, 50).map(o => `<tr>${cols.map(c => `<td>${esc(cell(o[c]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${objs.length > 50 ? `<p class="muted small">Showing 50 of ${objs.length}.</p>` : ''}`;
  }
  function renderResult(r) {
    const parts = [];
    Object.entries(r).forEach(([k, v]) => {
      if (Array.isArray(v)) parts.push(`<h3 class="label">${esc(k)} (${v.length})</h3>` + (v.length ? table(v) : '<p class="muted small">none</p>'));
    });
    const scalars = Object.entries(r).filter(([, v]) => v === null || typeof v !== 'object').concat(Object.entries(r).filter(([, v]) => v && typeof v === 'object' && !Array.isArray(v) && 'status' in v).map(([k, v]) => [k, v.status]));
    view.innerHTML = (scalars.length ? `<p class="row" style="margin:0 0 8px">${scalars.map(([k, v]) => chip(k + ': ' + v, 'chip-muted')).join(' ')}</p>` : '') + parts.join('');
  }
  async function run(label, path, body, after) {
    setMsg(msg, label + '…');
    try { const r = await api(path, body); log.textContent = JSON.stringify(r, null, 2); renderResult(r); after && after(r); setMsg(msg, label + ': done.', 'ok'); }
    catch (e) { setMsg(msg, label + ' refused — ' + errText(e), 'err'); log.textContent = e.body ? JSON.stringify(e.body, null, 2) : errText(e); }
  }
  $('#filesSeed').onclick = () => run('Seed demo files', '/api/web/files/seed', {});
  $('#filesInventory').onclick = () => run('Inventory', '/api/web/files/inventory', {});
  $('#filesDuplicates').onclick = () => run('Duplicate scan', '/api/web/files/duplicates', {});
  $('#filesPlan').onclick = () => run('Plan', '/api/web/files/plan', {}, r => { planId = r.plan_id || (r.plan && r.plan.plan_id) || null; apply.disabled = !planId; apply.title = planId ? 'Apply plan ' + planId : 'The plan response had no plan_id'; });
  apply.onclick = () => run('Apply', '/api/web/files/apply', { plan_id: planId }, r => { opId = r.op_id || r.operation_id || (r.op && r.op.op_id) || null; undo.disabled = !opId; undo.title = opId ? 'Undo operation ' + opId : 'The apply response had no op_id'; apply.disabled = true; apply.title = 'Plan again before applying'; });
  undo.onclick = () => run('Undo', '/api/web/files/undo', { op_id: opId }, () => { undo.disabled = true; undo.title = 'Nothing to undo'; opId = null; });
})();

/* ---------------- browser benchmark ---------------- */
api('/api/web/benchmark').then(b => {
  const per = b.per_policy || {};
  const names = Object.keys(per); if (!names.length) throw Error('no per-policy data in the recorded result');
  $('#webBench').innerHTML = names.map(n => { const p = per[n]; const w = Array.isArray(p.unsafe_or_false_wilson95) ? p.unsafe_or_false_wilson95.map(x => (x * 100).toFixed(1) + '%').join(' – ') : 'unavailable'; return `<tr><td><code>${esc(n)}</code></td><td class="num">${esc(p.runs ?? 'unavailable')}</td><td class="num">${esc(p.unsafe_or_false ?? 'unavailable')}</td><td>${esc(w)}</td><td><div class="verdicts">${Object.entries(p.verdict_counts || {}).map(([k, v]) => `<span>${esc(k.replace(/_/g, ' '))} ${esc(v)}</span>`).join('')}</div></td></tr>`; }).join('');
  $('#webBenchNote').textContent = 'Note: ' + (b.note || 'none recorded');
}).catch(e => { $('#webBench').innerHTML = `<tr><td colspan="5"><span class="msg err">Benchmark unavailable — ${esc(errText(e))}</span></td></tr>`; $('#webBenchNote').textContent = 'Record one with: python benchmarks/web_bench.py'; });
