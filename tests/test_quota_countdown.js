const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(
  path.join(__dirname, '../app/static/usage.js'),
  'utf8'
);
const now = 1_790_496_000;

function countdownContext(nodes = []) {
  const start = script.indexOf('function quotaResetText(');
  const end = script.indexOf('function renderQuotaWindow(');
  assert.ok(start >= 0 && end > start, 'quota countdown helpers must exist');
  const context = vm.createContext({
    Date: { now: () => now * 1000 },
    document: { querySelectorAll: () => nodes }
  });
  vm.runInContext(script.slice(start, end), context);
  return context;
}

test('the full usage-page script has valid syntax', () => {
  new vm.Script(script);
});

test('a partial week is not rounded back up to seven days', () => {
  const { quotaResetText } = countdownContext();
  assert.equal(
    quotaResetText(now + 604800 - 45 * 60),
    '6天 23小时 15分钟后重置'
  );
  assert.equal(
    quotaResetText(now + 604800 - 3 * 3600 - 15 * 60),
    '6天 20小时 45分钟后重置'
  );
  assert.equal(quotaResetText(now + 604800), '7天 0小时 0分钟后重置');
});

test('hours and minutes use remaining time rather than the quota duration', () => {
  const { quotaResetText } = countdownContext();
  assert.equal(quotaResetText(now + 5 * 3600 - 61), '4小时 58分钟后重置');
  assert.equal(quotaResetText(now + 3599), '59分钟后重置');
  assert.equal(quotaResetText(now + 60), '1分钟后重置');
  assert.equal(quotaResetText(now + 59), '不足1分钟后重置');
});

test('expired or awaiting-refresh windows never show a new countdown', () => {
  const { quotaResetText } = countdownContext();
  assert.equal(quotaResetText(now), '等待额度更新');
  assert.equal(quotaResetText(now - 3600), '等待额度更新');
  assert.equal(quotaResetText(now + 3600, true), '等待额度更新');
});

test('the countdown advances and expires without fetching quota again', () => {
  const node = {
    dataset: { resetsAt: String(now + 3660), awaitingRefresh: 'false' },
    textContent: ''
  };
  const context = countdownContext([node]);
  context.updateQuotaCountdowns();
  assert.equal(node.textContent, '1小时 1分钟后重置');
  context.Date.now = () => (now + 120) * 1000;
  context.updateQuotaCountdowns();
  assert.equal(node.textContent, '59分钟后重置');
  context.Date.now = () => (now + 3660) * 1000;
  context.updateQuotaCountdowns();
  assert.equal(node.textContent, '等待额度更新');
  node.dataset.awaitingRefresh = 'true';
  context.Date.now = () => now * 1000;
  context.updateQuotaCountdowns();
  assert.equal(node.textContent, '等待额度更新');
});

test('countdown updates avoid replacing unchanged text', () => {
  let writes = 0;
  const node = {
    dataset: { resetsAt: String(now + 60), awaitingRefresh: 'false' },
    get textContent() {
      return '1分钟后重置';
    },
    set textContent(value) {
      writes++;
    }
  };
  countdownContext([node]).updateQuotaCountdowns();
  assert.equal(writes, 0);
});

test('the independent countdown timer is cleaned up on page hide and re-entry', () => {
  const start = script.indexOf('let refreshTimer');
  assert.ok(start >= 0);
  const events = new Map();
  const timers = new Map();
  const document = { hidden: false };
  let nextId = 0;
  let countdownUpdates = 0;
  vm.runInNewContext(script.slice(start), {
    window: {
      addEventListener: (name, callback) => events.set(name, callback)
    },
    document,
    setInterval: (callback, delay) => {
      timers.set(++nextId, { callback, delay });
      return nextId;
    },
    clearInterval: id => timers.delete(id),
    clearTimeout() {},
    loginTimer: undefined,
    refreshAll: () => new Promise(() => {}),
    updateQuotaCountdowns() {
      countdownUpdates++;
    }
  });
  events.get('pageshow')();
  assert.equal(countdownUpdates, 1);
  assert.equal(
    timers.size,
    2,
    'network refresh and countdown need independent timers'
  );
  assert.deepEqual(
    [...timers.values()].map(timer => timer.delay).sort((a, b) => a - b),
    [1000, 15000]
  );
  const countdown = [...timers.values()].find(timer => timer.delay === 1000);
  countdown.callback();
  assert.equal(
    countdownUpdates,
    2,
    'a pending quota request must not stop the clock'
  );
  document.hidden = true;
  countdown.callback();
  assert.equal(countdownUpdates, 2, 'hidden pages do not need text updates');
  events.get('pageshow')();
  assert.equal(timers.size, 2, 're-entry must not duplicate timers');
  events.get('pagehide')();
  assert.equal(timers.size, 0);
});
