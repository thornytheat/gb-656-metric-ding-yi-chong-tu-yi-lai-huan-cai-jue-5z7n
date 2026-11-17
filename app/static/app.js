/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const MERGE_STATE = {
  mergeId: null,
  revision: 0,
  conflicts: [],
  selections: {},
};

const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async 'merge-open'(button) {
    const session = await request('/api/merges', {
      base_file: button.dataset.baseFile,
      left_file: button.dataset.leftFile,
      right_file: button.dataset.rightFile,
    });
    await loadMerge(session.merge_id);
    return `会话 ${session.merge_id}，冲突 ${session.conflicts.length}`;
  },

  async 'merge-save'() {
    const session = await request(`/api/merges/${MERGE_STATE.mergeId}/selections`, {
      selections: MERGE_STATE.selections,
      expected_revision: MERGE_STATE.revision,
    });
    MERGE_STATE.revision = session.revision;
    renderConflicts(session.conflicts);
    await refreshPreview();
    return `已保存选边，版本 ${session.revision}`;
  },

  async 'merge-publish'() {
    const result = await request(`/api/merges/${MERGE_STATE.mergeId}/publish`, {
      expected_revision: MERGE_STATE.revision,
    });
    setMergeStatus(`已发布批次 ${result.run_id}，序号 ${result.version_seq}`);
    setTimeout(() => window.location.reload(), 500);
    return `批次 ${result.run_id}`;
  },

  async 'merge-recover'() {
    const result = await request(`/api/merges/${MERGE_STATE.mergeId}/recover`, {});
    setMergeStatus(`恢复完成，批次 ${result.run_id}${result.reused ? '（复用，未重复生成）' : ''}`);
    return `批次 ${result.run_id}`;
  },
};

async function request(url, body, method) {
  const options = { method: method || (body === undefined ? 'GET' : 'POST') };
  if (options.method === 'POST') {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run' || name === 'merge-publish') {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});

/* ---------- 合并页 ---------- */

function setMergeStatus(text, bad) {
  const target = document.querySelector('[data-merge-status]');
  if (target) {
    target.textContent = text;
    target.classList.toggle('is-bad', Boolean(bad));
  }
}

function renderConflicts(conflicts) {
  const body = document.querySelector('[data-merge-conflicts]');
  if (!body) return;
  body.textContent = '';
  if (!conflicts.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'empty';
    cell.textContent = '没有冲突，可直接发布。';
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  conflicts.forEach((item) => {
    const row = document.createElement('tr');
    const cells = [
      item.table || '—',
      `${item.reason}（${item.kind}）`,
      item.left_label,
      item.right_label,
    ];    cells.forEach((text) => {
      const cell = document.createElement('td');
      cell.className = 'name';
      cell.textContent = text;
      row.appendChild(cell);
    });
    const choice = document.createElement('td');
    item.options.forEach((option) => {
      const label = document.createElement('label');
      label.className = 'field-inline';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = `choice-${item.key}`;
      radio.value = option;
      radio.checked = MERGE_STATE.selections[item.key] === option;
      radio.addEventListener('change', () => {
        MERGE_STATE.selections[item.key] = option;
        refreshPreview();
      });
      label.appendChild(radio);
      label.appendChild(document.createTextNode(option === 'take-left' ? '取左' : '取右'));
      choice.appendChild(label);
    });
    if (item.kind === 'post-merge-duplicate') {
      choice.textContent = '必须先删掉一条';
    }
    row.appendChild(choice);
    body.appendChild(row);
  });
}

function renderPreview(result) {
  const body = document.querySelector('[data-merge-preview]');
  if (!body) return;
  body.textContent = '';
  result.fields.forEach((field) => {
    const row = document.createElement('tr');
    const values = [
      field.table,
      field.name,
      field.type_raw,
      field.required ? '是' : '否',
      (field.sources && field.sources.picked) || '—',
    ];
    values.forEach((text) => {
      const cell = document.createElement('td');
      cell.className = 'name';
      cell.textContent = text;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  const pending = result.unresolved.length;
  const invalid = result.invalid.length;
  const dup = result.duplicates.length;
  setMergeStatus(
    pending || invalid || dup
      ? `未裁决 ${pending}，非法 ${invalid}，合成后重名 ${dup}：拒绝发布。`
      : `合成 ${result.fields.length} 个字段，可发布。`,
    Boolean(pending || invalid || dup),
  );
}

async function refreshPreview() {
  if (!MERGE_STATE.mergeId) return;
  const preview = await request(`/api/merges/${MERGE_STATE.mergeId}/preview`);
  renderPreview(preview.result);
}

async function loadMerge(mergeId) {
  const session = await request(`/api/merges/${mergeId}`);
  MERGE_STATE.mergeId = mergeId;
  MERGE_STATE.revision = session.selection_revision;
  MERGE_STATE.selections = Object.assign({}, session.selections || {});
  MERGE_STATE.conflicts = session.conflicts || [];
  const panel = document.querySelector('[data-merge-panel]');
  if (panel) panel.hidden = false;
  const title = document.querySelector('[data-merge-title]');
  if (title) title.textContent = mergeId;
  const revision = document.querySelector('[data-merge-revision]');
  if (revision) revision.textContent = String(MERGE_STATE.revision);
  renderConflicts(MERGE_STATE.conflicts);
  await refreshPreview();
}

document.addEventListener('click', (event) => {
  const row = event.target.closest('[data-merge-id]');
  if (!row) return;
  loadMerge(row.dataset.mergeId).catch((error) => {
    setMergeStatus(error.message, true);
  });
});
