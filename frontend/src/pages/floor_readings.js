import {request} from '../api.js';
import {actionForm} from '../controls.js';
import {el} from '../dom.js';
import {dataView, status, titleBlock} from '../widgets.js';

export const page = {
  title: '落点测读与踏点复核',
  module: '按踏点录入走向测值 · 网格领点',
  caption: '踏点领点与读数复核',
  detail: '测值必须对应胶带区内的厘米坐标，同一轮次同点只记录一次；冲突补传时两条读数均保留。后台分析仅使用审核且无争议记录。同区同批按 20 厘米网格派空位，优先补在当前两个异组最近踏点的中点附近。未交读的领点会保留，分组更新也不删除。',
  path: '/floor_readings',
  columns: [
    {name: 'code', label: '编号'},
    {name: 'panel', label: '地板块'},
    {name: 'survey', label: '走测轮次'},
    {name: 'x_cm', label: '点位 X cm'},
    {name: 'y_cm', label: '点位 Y cm'},
    {name: 'slide', label: '模拟滑步读数'},
    {name: 'capture_key', label: '轮次点位凭证'},
    {name: 'checked', label: '已复核'},
    {name: 'source', label: '来源'},
    {name: 'status', label: '状态'},
    {name: 'conflict_code', label: '冲突簿'},
  ],
  claimColumns: [
    {name: 'code', label: '领点号'},
    {name: 'panel', label: '区'},
    {name: 'survey', label: '批'},
    {name: 'x_cm', label: '网格 X'},
    {name: 'y_cm', label: '网格 Y'},
    {name: 'state_label', label: '领点状态'},
    {name: 'pair', label: '最近异组对'},
    {name: 'group_pair', label: '分组'},
    {name: 'grouping_code', label: '当前分组簿'},
    {name: 'claimed_by', label: '领点人'},
  ],
  actions: [
    {
      name: 'claim_point',
      label: '在 20 厘米网格领新点',
      path: '/floor_readings/claim-point',
      fields: [
        {name: 'panel', label: '地板块', kind: 'text', initial: 'ZONE-A'},
        {name: 'survey', label: '走测批次', kind: 'text', initial: 'SUR-E-01'},
        {name: 'code', label: '领点编号（留空自动生成）', kind: 'text', initial: '', required: false},
        {name: 'grouping_code', label: '指定分组簿（留空取当前）', kind: 'text', initial: '', required: false},
      ],
      help: '只使用当前已复核且无争议的两组读数；系统找最近异组踏点对，并派其中点附近最近的空网格位。凑不齐两组时会提示暂时派不了。两人同时领点也不会拿到同一坐标。',
      roles: ['operator', 'reviewer'],
      success: '已派网格点',
    },
    {
      name: 'record_step',
      label: '登记走向踏点',
      path: '/floor_readings/record-step',
      fields: [
        {name: 'code', label: '踏点号', kind: 'text', initial: 'STEP-A-QA'},
        {name: 'panel', label: '地板块', kind: 'text', initial: 'ZONE-A'},
        {name: 'survey', label: '走测轮次', kind: 'text', initial: 'SUR-E-01'},
        {name: 'x_cm', label: '点 X cm', kind: 'int', initial: 95},
        {name: 'y_cm', label: '点 Y cm', kind: 'int', initial: 90},
        {name: 'slide', label: '模拟滑步读数', kind: 'float', initial: 0.21},
        {name: 'source', label: '来源标记', kind: 'text', initial: '现场直录', required: false},
      ],
      help: '用厘米坐标和相对模拟读数复现测点。若与断网补传在同坐标相遇，两条读数都会保留并进入冲突复核。',
      roles: ['operator', 'reviewer'],
      success: '已写入',
    },
    {
      name: 'sync_step',
      label: '断网补传踏点',
      path: '/floor_readings/sync-step',
      fields: [
        {name: 'code', label: '补传踏点号', kind: 'text', initial: 'STEP-OFFLINE-QA'},
        {name: 'panel', label: '地板块', kind: 'text', initial: 'ZONE-A'},
        {name: 'survey', label: '走测轮次', kind: 'text', initial: 'SUR-E-01'},
        {name: 'x_cm', label: '点 X cm', kind: 'int', initial: 95},
        {name: 'y_cm', label: '点 Y cm', kind: 'int', initial: 90},
        {name: 'slide', label: '模拟滑步读数', kind: 'float', initial: 0.24},
        {name: 'source', label: '来源标记', kind: 'text', initial: '断网补传', required: false},
        {name: 'client_key', label: '补传幂等键（可留空）', kind: 'text', initial: '', required: false},
      ],
      help: '网络恢复后提交。若现场直录已占用同位置，不覆盖原读数，而是建立待复核冲突，暂停该位置入组。',
      roles: ['operator', 'reviewer'],
      success: '补传已处理',
    },
    {
      name: 'verify_step',
      label: '复核踏点量测',
      path: '/floor_readings/verify-step',
      fields: [
        {name: 'point', label: '踏点号', kind: 'text', initial: 'STEP-A-QA'},
      ],
      help: '复核员对照纸质走测记录后确认。若该踏点属于冲突，此操作等同于选定该条读数。',
      roles: ['reviewer'],
      success: '已写入',
    },
    {
      name: 'resolve_conflict',
      label: '按编号选定冲突读数',
      path: '/floor_readings/resolve-conflict',
      fields: [
        {name: 'code', label: '冲突簿编号', kind: 'text', initial: 'CONF-QA'},
        {name: 'selected_code', label: '选定的踏点号', kind: 'text', initial: 'STEP-OFFLINE-QA'},
      ],
      help: '也可以直接点击冲突卡片中的“选定此条并入组”。未选读数仍保留为未选用记录。',
      roles: ['reviewer'],
      success: '冲突已复核',
    },
  ],
};

export const {title} = page;
export const key = 'floor_readings';

function claimRows(claims) {
  return claims.map(row => ({
    ...row,
    pair: row.point_a ? `${row.point_a} ↔ ${row.point_b}` : '—',
    group_pair: row.group_a ? `${row.group_a} / ${row.group_b}` : '—',
  }));
}

function conflictCard(conflict, role, refresh) {
  const notice = status('');
  const open = conflict.state === 'open';
  const candidateTable = dataView(conflict.readings || [], [
    {name: 'code', label: '踏点号'},
    {name: 'slide', label: '读数'},
    {name: 'source', label: '来源'},
    {name: 'checked', label: '已复核'},
    {name: 'status', label: '状态'},
  ], 'table');

  const buttons = !open || role !== 'reviewer' ? null : el('div', {class: 'fields'},
    (conflict.readings || conflict.candidate_codes || []).map(reading => {
      const code = typeof reading === 'string' ? reading : reading.code;
      const button = el('button', {type: 'button'}, `选定 ${code} 并入组`);
      button.addEventListener('click', async () => {
        button.disabled = true;
        notice.className = 'status';
        notice.textContent = '正在提交复核选择…';
        try {
          await request('/floor_readings/resolve-conflict', {
            code: conflict.code,
            selected_code: code,
          });
          notice.textContent = '已选定，刷新后该读数恢复入组资格。';
          notice.classList.add('ok');
          await refresh();
        } catch (error) {
          button.disabled = false;
          notice.textContent = error.message;
          notice.classList.add('error');
        }
      });
      return button;
    }));

  return el('article', {class: 'record conflict-card', 'data-state': conflict.state},
    el('h3', {}, `${conflict.code} · ${conflict.status_label || conflict.state}`),
    el('p', {}, `${conflict.panel} / ${conflict.survey} · 坐标 (${conflict.x_cm}, ${conflict.y_cm})`),
    candidateTable,
    open && role !== 'reviewer'
      ? status('两条读数均已保留；该位置暂停派点依据和入组，等待复核员选定。', 'error')
      : null,
    !open ? el('p', {}, `已选定：${conflict.selected_code || '—'}`) : null,
    buttons,
    notice);
}

function paintBoard(root, data, role, refresh) {
  const openConflicts = data.conflicts.filter(item => item.state === 'open');
  root.replaceChildren(
    el('h2', {}, '领点台账与冲突复核'),
    el('section', {class: 'conflict-board', 'aria-label': '读数冲突'},
      el('h3', {}, `读数冲突（${openConflicts.length} 个待复核）`),
      data.conflicts.length
        ? el('div', {class: 'records grid'}, data.conflicts.map(item => conflictCard(item, role, refresh)))
        : status('暂无断网补传/现场直录冲突。')),
    el('section', {class: 'claim-board', 'aria-label': '领点记录'},
      el('h3', {}, `未交读与已派领点（${data.claims.length}）`),
      data.claims.length
        ? dataView(claimRows(data.claims), page.claimColumns, 'table')
        : status('还没有领点。下方填写区/批后即可按当前分组派点。')),
    el('section', {class: 'reading-board', 'aria-label': '全部读数'},
      el('h3', {}, '踏点读数台账'),
      data.readings.length
        ? dataView(data.readings, page.columns, 'table')
        : status('暂无踏点读数。')),
  );
}

export async function render(root, role) {
  const board = el('section', {class: 'ledger floor-board', 'aria-label': '踏点领点工作台'});
  const forms = el('section', {class: 'actions floor-forms', 'aria-label': '踏点操作'});
  root.replaceChildren(
    titleBlock(page.module, page.title, page.detail),
    el('div', {class: 'work stack floor-readings-page'}, board, forms),
  );

  async function refresh() {
    try {
      const [readings, claims, conflicts] = await Promise.all([
        request(page.path),
        request('/floor_readings/claims'),
        request('/floor_readings/conflicts'),
      ]);
      paintBoard(board, {readings, claims, conflicts}, role, refresh);
    } catch (error) {
      board.replaceChildren(status(error.message, 'error'));
    }
  }

  const actions = page.actions.filter(action => action.roles.includes(role));
  forms.replaceChildren(...actions.map(action => actionForm(action, refresh)));
  await refresh();
}
