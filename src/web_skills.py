"""W8: recorded, versioned, approval-gated web skills.

A skill is compiled from a REAL supervised Playwright demonstration on the CRM fixture:
an init script records DOM events (input/change/click/submit) with semantic target
descriptors; the effect is verified straight from crm.sqlite3; the event stream is then
compiled into typed steps with semantic locators (label/role primary, name/css fallbacks),
parameter placeholders, preconditions and verification predicates.

Rules enforced here:
- approved versions are immutable (SQLite triggers refuse body updates and status changes
  away from APPROVED); repairs are proposed as NEW versions in PENDING_REVIEW;
- replay refuses unapproved versions, bad params and failed preconditions (read-only check);
- a skill_runs row is persisted as DISPATCHING before the submit click (the side effect);
- success is decided only by reading crm.sqlite3, never the page toast.

Browser actions are DOM-level (Playwright locators), NOT physical cursor movement.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHROMIUM = '/opt/pw-browsers/chromium'
NON_DIGEST_FIELDS = {'approval_status', 'digest', 'approved_at'}
PARAM_TYPES = {'str': str}
SQL_IDENT = re.compile(r'^[a-z_][a-z0-9_]*$')
ALLOWED_PREDICATE_TABLES = {('crm', 'tasks'), ('crm', 'contacts')}

CAPTURE_JS = r"""
(() => {
  if (window.__rec) return;
  window.__rec = [];
  let next = 1; const ids = new WeakMap();
  const idOf = el => { if (!ids.has(el)) ids.set(el, next++); return ids.get(el); };
  const roleOf = el => {
    if (el.getAttribute('role')) return el.getAttribute('role');
    const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'button' || (t === 'input' && ['submit', 'button', 'reset'].includes(ty))) return 'button';
    if (t === 'textarea' || (t === 'input' && ['', 'text', 'email', 'search'].includes(ty))) return 'textbox';
    if (t === 'select') return 'combobox';
    return null;
  };
  const nameOf = el => {
    const a = el.getAttribute('aria-label'); if (a) return a.trim();
    if (el.labels && el.labels.length) {
      const c = el.labels[0].cloneNode(true); c.querySelectorAll('input,select,textarea,button').forEach(n => n.remove());
      return c.textContent.trim();
    }
    const t = el.tagName.toLowerCase();
    if (t === 'button') return (el.innerText || el.textContent || '').trim();
    if (t === 'input' && ['submit', 'button'].includes((el.type || '').toLowerCase())) return el.value;
    return null;
  };
  const desc = el => ({
    rec_id: idOf(el), tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase() || null,
    role: roleOf(el), accessible_name: nameOf(el), testid: el.getAttribute('data-testid'),
    name: el.getAttribute('name'), id: el.id || null, form_id: el.form ? (el.form.id || null) : null,
  });
  window.__resolveDescribe = desc;
  const log = (kind, el, extra) => window.__rec.push(Object.assign({kind, t: performance.timeOrigin + performance.now(), target: desc(el)}, extra || {}));
  for (const kind of ['input', 'change']) document.addEventListener(kind, e => {
    const el = e.target; if (!el || !el.tagName) return; log(kind, el, {value: el.value});
  }, true);
  document.addEventListener('click', e => {
    const el = e.target && e.target.closest ? e.target.closest('button,input,a,[role=button]') : null; if (el) log('click', el);
  }, true);
  document.addEventListener('submit', e => {
    const f = e.target;
    const hidden = Array.from(f.querySelectorAll('input[type=hidden]')).map(h => h.getAttribute('name')).filter(Boolean);
    log('submit', f, {hidden_fields: hidden});
  }, true);
})();
"""

SCAN_JS = r"""
() => Array.from(document.querySelectorAll('input,textarea,select,button')).filter(el => {
  if ((el.getAttribute('type') || '').toLowerCase() === 'hidden') return false;
  const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
}).map(el => window.__resolveDescribe(el))
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def skill_digest(skill: dict) -> str:
    """Content digest of a skill version; excludes review state so export/import preserve it."""
    return sha256_json({k: v for k, v in skill.items() if k not in NON_DIGEST_FIELDS})


class SkillError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra

    def as_dict(self) -> dict:
        return {'code': self.code, 'message': str(self), **self.extra}


def _fmt(template: Any, params: dict) -> Any:
    if not isinstance(template, str):
        return template
    out = template
    for k, v in params.items():
        out = out.replace('{' + k + '}', str(v))
    return out


def _tokens(s: str | None) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', (s or '').lower()))


class SkillLibrary:
    def __init__(self, db_path: str | Path, headless: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        with self._db() as db:
            db.executescript("""
CREATE TABLE IF NOT EXISTS web_skills(
  skill_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
  digest TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT, PRIMARY KEY(skill_id, version));
CREATE TRIGGER IF NOT EXISTS web_skills_body_immutable BEFORE UPDATE OF body, digest, skill_id, version ON web_skills
BEGIN SELECT RAISE(ABORT, 'skill version body is immutable'); END;
CREATE TRIGGER IF NOT EXISTS web_skills_approved_frozen BEFORE UPDATE OF status ON web_skills WHEN OLD.status = 'APPROVED'
BEGIN SELECT RAISE(ABORT, 'approved skill version is immutable'); END;
CREATE TABLE IF NOT EXISTS web_skill_runs(
  run_id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER, params TEXT, op_id TEXT, status TEXT,
  result TEXT, created_at TEXT, updated_at TEXT);
""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    # ---- browser ---------------------------------------------------------------
    def _browser(self):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        from web_mission import browser_launch_options
        launch: dict[str, Any] = browser_launch_options(self.headless)
        try:
            return pw, pw.chromium.launch(**launch)
        except Exception:
            pw.stop()
            raise

    @staticmethod
    def _open_crm(browser, url: str):
        page = browser.new_page()
        page.add_init_script(CAPTURE_JS)
        page.goto(url)
        page.wait_for_load_state('networkidle')
        dismiss = page.get_by_role('button', name='Dismiss', exact=True)
        if dismiss.count():
            dismiss.click()  # popup fault: dismiss the modal, never click through it
        return page

    # ---- read-only fixture DB access ---------------------------------------------
    @staticmethod
    def _ro(fixture_root: Path, app: str) -> sqlite3.Connection:
        p = Path(fixture_root) / f'{app}.sqlite3'
        db = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _eval_predicate(self, pred: dict, params: dict, fixture_root: Path) -> dict:
        if pred.get('kind') != 'sql_row_exists' or (pred.get('db'), pred.get('table')) not in ALLOWED_PREDICATE_TABLES:
            return {'predicate': pred, 'holds': False, 'error': 'unsupported predicate'}
        where = {k: _fmt(v, params) for k, v in pred['where'].items()}
        if not all(SQL_IDENT.match(k) for k in where):
            return {'predicate': pred, 'holds': False, 'error': 'bad column'}
        sql = f"SELECT * FROM {pred['table']} WHERE " + ' AND '.join(f'{k}=?' for k in where)
        db = self._ro(fixture_root, pred['db'])
        try:
            rows = [dict(r) for r in db.execute(sql, tuple(where.values()))]
        finally:
            db.close()
        return {'predicate': {**pred, 'where': where}, 'holds': len(rows) == int(pred.get('expect_count', 1)),
                'row_count': len(rows), 'rows': rows, 'source': f"{pred['db']}.sqlite3"}

    def _verify(self, skill: dict, params: dict, fixture_root: Path, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            results = [self._eval_predicate(p, params, fixture_root) for p in skill['verification_predicates']]
            ok = all(r['holds'] for r in results)
            if ok or time.monotonic() >= deadline:
                return {'status': 'CONFIRMED' if ok else 'FAILED', 'predicates': results, 'checked_at': now()}
            time.sleep(0.2)

    # ---- record & compile ----------------------------------------------------------
    def record(self, urls: dict, fixture_root: Path, contact_id: str, title: str, skill_id: str | None = None) -> dict:
        """Supervised demonstration in a real browser, verified from crm.sqlite3, compiled into a skill."""
        fixture_root = Path(fixture_root)
        demo_op = f'demo-{uuid.uuid4().hex}'
        pw, browser = self._browser()
        try:
            page = self._open_crm(browser, urls['crm'])
            page.get_by_test_id('contact-row').first.wait_for(timeout=10000)
            page.get_by_label('Contact ID', exact=True).fill(contact_id)
            page.get_by_label('Task title', exact=True).fill(title)
            # demo-only correlation key (programmatic, so it is not part of the recorded user events)
            page.evaluate("op => document.querySelector('#f [name=op_id]').value = op", demo_op)
            page.get_by_role('button', name='Create task', exact=True).click()
            deadline = time.monotonic() + 10
            while not page.evaluate("() => window.__rec.some(e => e.kind === 'submit')") and time.monotonic() < deadline:
                time.sleep(0.05)
            events = page.evaluate('() => window.__rec')
        finally:
            browser.close()
            pw.stop()
        demo_skill = {'verification_predicates': [{'kind': 'sql_row_exists', 'db': 'crm', 'table': 'tasks',
                                                   'where': {'contact_id': contact_id, 'title': title, 'op_id': demo_op}}]}
        effect = self._verify(demo_skill, {}, fixture_root)
        if effect['status'] != 'CONFIRMED':
            raise SkillError('DEMONSTRATION_NOT_VERIFIED', 'demonstration effect not found in crm.sqlite3; nothing compiled', verification=effect)
        skill = self.compile(events, {'contact_id': contact_id, 'title': title}, skill_id=skill_id or f'skill-crm-followup-{uuid.uuid4().hex[:8]}')
        skill['source_provenance']['demonstration_verification'] = {'status': 'CONFIRMED', 'task': effect['predicates'][0]['rows'][0]['id'],
                                                                    'source': 'crm.sqlite3'}
        self._insert(skill, 'PENDING_REVIEW')
        return self.get(skill['skill_id'], skill['version'])

    @staticmethod
    def compile(events: list[dict], literals: dict[str, str], skill_id: str) -> dict:
        def param_of(value: str) -> str:
            for k, v in literals.items():
                if value == v:
                    return '{' + k + '}'
            return value

        fills: dict[int, dict] = {}
        order: list[tuple[str, int, dict]] = []
        for e in events:
            tgt = e['target']
            if e['kind'] in ('input', 'change') and tgt['tag'] in ('input', 'textarea', 'select') and tgt.get('type') != 'hidden':
                if tgt['rec_id'] not in fills:
                    order.append(('fill', tgt['rec_id'], tgt))
                fills[tgt['rec_id']] = e
            elif e['kind'] == 'click' and tgt.get('role') == 'button':
                order.append(('click', tgt['rec_id'], tgt))
        submit = next((e for e in events if e['kind'] == 'submit'), None)
        steps: list[dict] = []
        for action, rid, tgt in order:
            sig = {k: tgt.get(k) for k in ('tag', 'type', 'role', 'accessible_name', 'testid', 'name', 'id', 'form_id')}
            fallbacks = []
            if tgt.get('testid'):
                fallbacks.append({'by': 'testid', 'value': tgt['testid']})
            if action == 'fill':
                primary = {'by': 'label', 'value': tgt['accessible_name']} if tgt.get('accessible_name') else {'by': 'name', 'value': tgt['name']}
                if tgt.get('name') and primary['by'] != 'name':
                    fallbacks.append({'by': 'name', 'value': tgt['name']})
                steps.append({'action': 'fill', 'locator': primary, 'fallbacks': fallbacks, 'value': param_of(fills[rid]['value']), 'target': sig})
            else:
                if submit and tgt.get('type') == 'submit':
                    for h in submit.get('hidden_fields', []):
                        if h == 'op_id':  # app-provided idempotency key: bind a fresh one per replay
                            steps.append({'action': 'set_idempotency_key', 'locator': {'by': 'name', 'value': 'op_id'},
                                          'fallbacks': ([{'by': 'css', 'value': f"#{tgt['form_id']} [name=op_id]"}] if tgt.get('form_id') else []),
                                          'value': '{op_id}', 'target': {'tag': 'input', 'type': 'hidden', 'name': 'op_id', 'form_id': tgt.get('form_id')}})
                if tgt.get('form_id') and tgt.get('type') == 'submit':
                    fallbacks.append({'by': 'css', 'value': f"#{tgt['form_id']} [type=submit]"})
                primary = {'by': 'role', 'role': 'button', 'name': tgt['accessible_name']}
                steps.append({'action': 'click', 'locator': primary, 'fallbacks': fallbacks, 'target': sig, 'submits_form': bool(submit)})
        used = {k for k in literals if any(s.get('value') == '{' + k + '}' for s in steps)}
        if used != set(literals):
            raise SkillError('PARAMETERIZATION_FAILED', 'recorded values did not match every declared parameter', missing=sorted(set(literals) - used))
        events_min = [{'kind': e['kind'], 't': e['t'], 'target': e['target'], **({'value': e['value']} if 'value' in e else {})} for e in events]
        return {
            'skill_id': skill_id, 'version': 1, 'name': 'Create CRM follow-up task',
            'source_provenance': {'recorded_at': now(), 'event_count': len(events), 'events_digest': sha256_json(events_min),
                                  'event_kinds': [e['kind'] for e in events], 'method': 'playwright supervised demonstration (DOM-level)'},
            'parameters_schema': {k: 'str' for k in literals},
            'preconditions': [{'kind': 'contact_exists', 'param': 'contact_id'}],
            'steps': steps,
            'expected_effects': [{'kind': 'crm_task', 'contact_id': '{contact_id}', 'title': '{title}'}],
            'verification_predicates': [{'kind': 'sql_row_exists', 'db': 'crm', 'table': 'tasks', 'expect_count': 1,
                                         'where': {'contact_id': '{contact_id}', 'title': '{title}', 'op_id': '{op_id}'}}],
            'permissions': ['crm.write'], 'risk_class': 'LOCAL_REVERSIBLE',
            'approval_status': 'PENDING_REVIEW', 'learning_history': [],
        }

    # ---- storage ---------------------------------------------------------------------
    def _insert(self, skill: dict, status: str) -> None:
        body = {k: v for k, v in skill.items() if k not in NON_DIGEST_FIELDS}
        with self._db() as db:
            try:
                db.execute('INSERT INTO web_skills(skill_id,version,status,body,digest,created_at) VALUES(?,?,?,?,?,?)',
                           (skill['skill_id'], int(skill['version']), status, canonical(body), skill_digest(body), now()))
            except sqlite3.IntegrityError:
                raise SkillError('VERSION_EXISTS', f"{skill['skill_id']} v{skill['version']} already exists")

    def _row_to_skill(self, r: sqlite3.Row) -> dict:
        s = json.loads(r['body'])
        s.update(approval_status=r['status'], digest=r['digest'], approved_at=r['approved_at'])
        return s

    def get(self, skill_id: str, version: int | None = None) -> dict:
        with self._db() as db:
            if version is None:
                r = db.execute('SELECT * FROM web_skills WHERE skill_id=? ORDER BY version DESC LIMIT 1', (skill_id,)).fetchone()
            else:
                r = db.execute('SELECT * FROM web_skills WHERE skill_id=? AND version=?', (skill_id, int(version))).fetchone()
        if not r:
            raise SkillError('NOT_FOUND', f'no skill {skill_id} v{version}')
        return self._row_to_skill(r)

    def list(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute('SELECT skill_id,version,status,digest,created_at,approved_at FROM web_skills ORDER BY skill_id,version').fetchall()
        return [dict(r) for r in rows]

    def approve(self, skill_id: str, version: int) -> dict:
        s = self.get(skill_id, version)
        if s['approval_status'] != 'PENDING_REVIEW':
            raise SkillError('NOT_PENDING', f"v{version} is {s['approval_status']}")
        with self._db() as db:
            db.execute("UPDATE web_skills SET status='APPROVED', approved_at=? WHERE skill_id=? AND version=? AND status='PENDING_REVIEW'",
                       (now(), skill_id, int(version)))
        return self.get(skill_id, version)

    def export(self, skill_id: str, version: int) -> dict:
        s = self.get(skill_id, version)
        if skill_digest(s) != s['digest']:
            raise SkillError('CORRUPT', 'stored skill does not match its digest')
        return {'format': 'resolve.web_skill/1', 'exported_at': now(), 'digest': s['digest'], 'skill': s}

    def import_skill(self, data: dict) -> dict:
        skill = copy.deepcopy(data.get('skill') or {})
        if data.get('format') != 'resolve.web_skill/1' or not skill.get('skill_id'):
            raise SkillError('BAD_FORMAT', 'not a resolve web skill export')
        actual = skill_digest(skill)
        if actual != data.get('digest') or actual != skill.get('digest', actual):
            raise SkillError('DIGEST_MISMATCH', 'skill content does not match its digest; refusing import', expected=data.get('digest'), actual=actual)
        try:
            existing = self.get(skill['skill_id'], skill['version'])
        except SkillError:
            existing = None
        if existing:
            if existing['digest'] == actual:
                return existing
            raise SkillError('VERSION_EXISTS', 'a different skill with this id/version already exists')
        self._insert(skill, 'PENDING_REVIEW')  # imported skills are never pre-approved
        return self.get(skill['skill_id'], skill['version'])

    # ---- replay -----------------------------------------------------------------------
    @staticmethod
    def _locate(page, loc: dict, need_visible: bool = True):
        by = loc.get('by')
        if by == 'label':
            l = page.get_by_label(loc['value'], exact=True)
        elif by == 'role':
            l = page.get_by_role(loc['role'], name=loc['name'], exact=True)
        elif by == 'testid':
            l = page.get_by_test_id(loc['value'])
        elif by == 'name':
            l = page.locator(f'[name="{loc["value"]}"]')
        elif by == 'css':
            l = page.locator(loc['value'])
        else:
            return None
        if l.count() != 1:
            return None
        if need_visible and not (l.is_visible() and l.is_enabled()):
            return None
        return l

    def _resolve(self, page, step: dict):
        need_visible = step['action'] != 'set_idempotency_key'
        for i, loc in enumerate([step['locator'], *step.get('fallbacks', [])]):
            l = self._locate(page, loc, need_visible)
            if l is not None:
                return l, ('primary' if i == 0 else f'fallback[{i - 1}]:{loc["by"]}'), loc
        return None, None, None

    def _derive_step(self, page, step: dict) -> tuple[dict | None, list[dict]]:
        """Re-derive a step's locator from the CURRENT page by semantic similarity to its recorded target."""
        sig = step.get('target') or {}
        cands = page.evaluate(SCAN_JS)
        want_name = sig.get('accessible_name') or step['locator'].get('value') or step['locator'].get('name')
        scored = []
        for c in cands:
            s = 0.0
            if sig.get('name') and c.get('name') == sig['name']:
                s += 3
            if c.get('tag') == sig.get('tag'):
                s += 1
            if sig.get('type') and c.get('type') == sig['type']:
                s += 1
            if c.get('role') == (sig.get('role') or ('button' if step['action'] == 'click' else 'textbox')):
                s += 1
            if sig.get('form_id') and c.get('form_id') == sig['form_id']:
                s += 1
            a, b = _tokens(want_name), _tokens(c.get('accessible_name'))
            if a and b:
                s += 2 * len(a & b) / len(a | b)
            scored.append({'score': round(s, 3), 'candidate': c})
        scored.sort(key=lambda x: -x['score'])
        if not scored or scored[0]['score'] < 3 or (len(scored) > 1 and scored[1]['score'] == scored[0]['score']):
            return None, scored[:5]
        c = scored[0]['candidate']
        fallbacks = []
        if c.get('testid'):
            fallbacks.append({'by': 'testid', 'value': c['testid']})
        if step['action'] == 'click':
            primary = {'by': 'role', 'role': 'button', 'name': c['accessible_name']}
            if c.get('form_id') and c.get('type') == 'submit':
                fallbacks.append({'by': 'css', 'value': f"#{c['form_id']} [type=submit]"})
        else:
            primary = {'by': 'label', 'value': c['accessible_name']} if c.get('accessible_name') else {'by': 'name', 'value': c['name']}
            if c.get('name') and primary['by'] != 'name':
                fallbacks.append({'by': 'name', 'value': c['name']})
        new = {**step, 'locator': primary, 'fallbacks': fallbacks}
        if self._resolve(page, new)[0] is None:
            return None, scored[:5]
        return new, scored[:5]

    def _propose_repair(self, page, skill: dict, failed_index: int, reason: str) -> dict | None:
        steps = copy.deepcopy(skill['steps'])
        changes = []
        for i in range(failed_index, len(steps)):
            if self._resolve(page, steps[i])[0] is not None:
                continue
            new, evidence = self._derive_step(page, steps[i])
            if new is None:
                return None
            changes.append({'step': i, 'old_locator': steps[i]['locator'], 'new_locator': new['locator'],
                            'new_fallbacks': new['fallbacks'], 'top_candidates': evidence})
            steps[i] = new
        with self._db() as db:
            latest = db.execute('SELECT MAX(version) v FROM web_skills WHERE skill_id=?', (skill['skill_id'],)).fetchone()['v']
        proposal = {k: copy.deepcopy(v) for k, v in skill.items() if k not in NON_DIGEST_FIELDS}
        proposal['version'] = int(latest) + 1
        proposal['steps'] = steps
        proposal['learning_history'] = [*skill.get('learning_history', []),
                                        {'at': now(), 'from_version': skill['version'], 'failed_step': failed_index,
                                         'reason': reason, 'changes': changes, 'derived_from': 'live page semantic scan'}]
        self._insert(proposal, 'PENDING_REVIEW')
        return self.get(skill['skill_id'], proposal['version'])

    def _run(self, run_id: str, status: str, result: dict | None = None, **create) -> None:
        with self._db() as db:
            if create:
                db.execute('INSERT INTO web_skill_runs VALUES(?,?,?,?,?,?,?,?,?)',
                           (run_id, create['skill_id'], create['version'], canonical(create['params']), create['op_id'], status, None, now(), now()))
            else:
                db.execute('UPDATE web_skill_runs SET status=?, result=?, updated_at=? WHERE run_id=?',
                           (status, canonical(result) if result is not None else None, now(), run_id))

    def runs(self, skill_id: str | None = None) -> list[dict]:
        with self._db() as db:
            rows = db.execute('SELECT * FROM web_skill_runs WHERE (? IS NULL OR skill_id=?) ORDER BY created_at', (skill_id, skill_id)).fetchall()
        return [dict(r) for r in rows]

    def replay(self, skill_id: str, params: dict, urls: dict, fixture_root: Path, version: int | None = None) -> dict:
        fixture_root = Path(fixture_root)
        if version is None:
            with self._db() as db:
                r = db.execute("SELECT MAX(version) v FROM web_skills WHERE skill_id=? AND status='APPROVED'", (skill_id,)).fetchone()
            if r['v'] is None:
                return self._refused(skill_id, None, 'NOT_APPROVED', 'no approved version of this skill')
            version = r['v']
        skill = self.get(skill_id, version)
        base = {'skill_id': skill_id, 'version': skill['version']}
        if skill['approval_status'] != 'APPROVED':
            return self._refused(skill_id, skill['version'], 'NOT_APPROVED', f"v{skill['version']} is {skill['approval_status']}")
        if skill_digest(skill) != skill['digest']:
            return self._refused(skill_id, skill['version'], 'DIGEST_MISMATCH', 'stored skill content changed after approval')
        schema = skill['parameters_schema']
        bad = sorted(set(params) ^ set(schema)) + [k for k, t in schema.items() if k in params and not isinstance(params[k], PARAM_TYPES[t])]
        if bad:
            return self._refused(skill_id, skill['version'], 'INVALID_PARAMS', 'params do not match parameters_schema', fields=bad)
        for pre in skill['preconditions']:
            if pre['kind'] == 'contact_exists':
                db = self._ro(fixture_root, 'crm')
                try:
                    found = db.execute('SELECT id FROM contacts WHERE id=?', (params[pre['param']],)).fetchone()
                finally:
                    db.close()
                if not found:
                    return self._refused(skill_id, skill['version'], 'PRECONDITION_FAILED', f"contact {params[pre['param']]} does not exist",
                                         precondition=pre, source='crm.sqlite3 (read-only)')
            else:
                return self._refused(skill_id, skill['version'], 'PRECONDITION_UNKNOWN', f"unsupported precondition {pre['kind']}")
        op_id = f'skill-{uuid.uuid4().hex}'
        bound = {**params, 'op_id': op_id}
        run_id = f'run-{uuid.uuid4().hex[:12]}'
        self._run(run_id, 'PREPARED', skill_id=skill_id, version=skill['version'], params=params, op_id=op_id)
        trace: list[dict] = []
        dispatched = False
        pw, browser = self._browser()
        try:
            page = self._open_crm(browser, urls['crm'])
            for i, step in enumerate(skill['steps']):
                l, strategy, loc = self._resolve(page, step)
                if l is None:
                    reason = f"no unique {'visible ' if step['action'] != 'set_idempotency_key' else ''}element for step {i} via primary or {len(step.get('fallbacks', []))} fallback(s)"
                    proposal = self._propose_repair(page, skill, i, reason)
                    result = {**base, 'status': 'FAILED', 'failed_step': i, 'reason': reason, 'steps': trace, 'op_id': op_id,
                              'repair_proposal': proposal, 'side_effect_dispatched': False}
                    self._run(run_id, 'FAILED', result)
                    return result
                value = _fmt(step.get('value'), bound)
                if step['action'] == 'fill':
                    l.fill(value)
                elif step['action'] == 'set_idempotency_key':
                    l.evaluate('(el, v) => { el.value = v; }', value)
                elif step['action'] == 'click':
                    if step.get('submits_form'):
                        self._run(run_id, 'DISPATCHING')  # persisted before the side effect
                        dispatched = True
                    l.click()
                trace.append({'index': i, 'action': step['action'], 'strategy_used': strategy, 'locator': loc})
        except Exception as e:
            # before dispatch nothing was submitted: FAILED. After dispatch the outcome is unknown:
            # reconcile read-only; never re-dispatch.
            result = {**base, 'status': 'UNCERTAIN' if dispatched else 'FAILED', 'reason': f'{type(e).__name__}: {e}', 'steps': trace,
                      'op_id': op_id, 'side_effect_dispatched': dispatched}
            if dispatched:
                result['verification'] = self._verify(skill, bound, fixture_root, timeout=3)
                if result['verification']['status'] == 'CONFIRMED':
                    result['status'] = 'CONFIRMED'
            self._run(run_id, result['status'], result)
            return result
        finally:
            browser.close()
            pw.stop()
        verification = self._verify(skill, bound, fixture_root)
        result = {**base, 'status': verification['status'], 'steps': trace, 'verification': verification, 'op_id': op_id, 'run_id': run_id}
        self._run(run_id, result['status'], result)
        return result

    def _refused(self, skill_id, version, code, message, **extra) -> dict:
        return {'status': 'REFUSED', 'skill_id': skill_id, 'version': version, 'refusal': {'code': code, 'message': message, **extra},
                'side_effect_dispatched': False}
