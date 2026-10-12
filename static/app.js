/* Chunk boundaries follow retained action rows; media time follows actual timestamps. */
'use strict';
const $ = id => document.getElementById(id);
const videos = [0, 1, 2].map(i => $(`video${i}`));
const videoFormat = videos[0].canPlayType('video/mp4; codecs="avc1.42E01E"') ? 'mp4' : 'webm';
const state = {datasets: [], session: null, episodes: [], ep: null, points: [], flags: [], saved: [], savedFlags: [], chunk: 0, chunkView: 'start', action: 0, busy: false, generation: 0, playing: false, chunkStop: null, master: null, goalTime: 0, goalMediaTime: 0, drag: null, scrubbing: null, chunkPlaying: null, playbackGeneration: 0, backendReady: false};
state.anchors = new Set(); state.anchorCache = new Map(); state.selectedPoint = 1;
let toastTimer;
function toast(message, error = false) { $('toast').textContent = message; $('toast').classList.toggle('error', error); $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, error ? 9000 : 4000); }
async function api(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}
function route(suffix = '') { return `/api/session/${state.session.session}/episode/${state.ep.episode_index}${suffix}`; }
function dirty() { return !!state.ep && (state.points.some((v, i) => v !== state.saved[i]) || state.flags.some((v, i) => v !== state.savedFlags[i])); }
function canLeave() { return !dirty() || confirm('当前 episode 有未保存修改。确定放弃这些修改并切换吗？'); }
function format(value, places = 4) { return Number(value).toFixed(places); }
function category(value) { return value === 'failure' || value === 'fail' ? 'fail' : value === 'unknown' ? '未分类' : value; }
function categoryClass(value) { return value === 'fail' ? 'failure' : value; }
function updateControls() {
  const changed = dirty();
  $('save').disabled = !state.ep || state.busy || !state.backendReady;
  $('restore').disabled = !state.ep || state.busy;
  $('openDataset').disabled = state.busy || !state.backendReady;
  $('progressInput').disabled = state.busy;
  $('deltaInput').disabled = state.busy;
  $('resetPoint').disabled = state.busy;
  $('resetEpisode').disabled = !state.ep || state.busy;
  $('confirmRestore').disabled = state.busy;
  renderAnchorControls();
  $('curve').style.pointerEvents = state.busy ? 'none' : '';
  $('dirtyStatus').textContent = state.busy ? '正在处理…' : changed ? '有未保存的修改' : state.ep?.modified ? '此轨迹已修改 · 已保存' : '此轨迹未修改 · 可直接保存确认';
  $('dirtyStatus').classList.toggle('dirty', changed);
  if (state.ep) {
    const count = state.points.slice(1).filter((v, i) => Math.abs(v - state.ep.baseline_points[i + 1]) > 1e-7).length;
    $('annotatedSummary').textContent = `${state.ep.chunks.length} chunks · ${state.ep.modified ? '已修改' : '未修改'} · ${count} 段数值经过修改`;
    $('saveDetails').textContent = '保存即人工确认整条轨迹，所有 chunk 标记为 human_annotated=true';
  }
}
function populateLabelSets(item) {
  const select = $('labelSet'); select.replaceChildren();
  if (item?.labelsets.length) {
    for (const set of item.labelsets) { const opt = document.createElement('option'); opt.value = set.name; opt.textContent = set.name; select.append(opt); }
  } else { const opt = document.createElement('option'); opt.value = ''; opt.textContent = '无已有标签 · 使用线性初值'; select.append(opt); }
}
async function init() {
  try {
    const value = await api('/api/datasets'); state.datasets = value.datasets;
    state.backendReady = value.annotation_ui_version >= 3;
    for (const item of value.datasets) { const option = document.createElement('option'); option.value = item.path; option.textContent = `${item.name} (${item.episodes})`; $('datasetSelect').append(option); }
    $('datasetPath').placeholder = `${value.root}/数据集目录`;
    if (!state.backendReady) {
      $('datasetSummary').textContent = '请先重启标注服务，加载整条轨迹人工确认的保存规则';
      toast('界面已更新，请重启标注服务后刷新页面。', true);
    }
    updateControls();
  } catch (error) { toast(error.message, true); }
}
$('datasetSelect').addEventListener('change', () => { const item = state.datasets.find(d => d.path === $('datasetSelect').value); if (item) $('datasetPath').value = item.path; populateLabelSets(item); });
$('datasetPath').addEventListener('change', async () => {
  const path = $('datasetPath').value.trim(), item = state.datasets.find(d => d.path === path);
  populateLabelSets(item); $('datasetSelect').value = item?.path || '';
  if (!item && path) {
    try { const result = await api(`/api/inspect?path=${encodeURIComponent(path)}`); if ($('datasetPath').value.trim() === path) populateLabelSets(result); }
    catch { /* Opening the dataset reports a precise path error. */ }
  }
});
$('openDataset').addEventListener('click', async () => {
  if (state.busy || !canLeave()) return;
  const path = $('datasetPath').value.trim(); if (!path) { toast('请选择或输入数据集目录。', true); return; }
  state.busy = true; updateControls();
  try {
    const session = await api('/api/open', {path, source_set: $('labelSet').value || null});
    pause(); state.generation++; state.ep = null; state.session = session; state.episodes = session.episodes;
    $('datasetSummary').textContent = session.dataset_name; $('datasetSummary').title = session.dataset_path;
    $('datasetSettings').open = false;
    $('episodeCount').textContent = state.episodes.length; $('episodeSearch').value = ''; $('categoryFilter').value = 'all'; $('labelFilter').value = 'all';
    state.busy = false;
    renderEpisodes();
    if (state.episodes.length) await loadEpisode(state.episodes[0].episode_index, true);
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; updateControls(); }
});
function renderEpisodes() {
  const list = $('episodeList'); list.replaceChildren();
  const query = $('episodeSearch').value.trim(), cat = $('categoryFilter').value, mode = $('labelFilter').value;
  let count = 0;
  $('reviewSummary').textContent = `已修改 ${state.episodes.filter(e => e.modified).length} / ${state.episodes.length}`;
  for (const ep of state.episodes) {
    if (query && !String(ep.episode_index).padStart(6, '0').includes(query)) continue;
    if (cat !== 'all' && ep.category !== cat && !(cat === 'failure' && ep.category === 'fail')) continue;
    if (mode === 'existing' && !ep.has_labels || mode === 'missing' && ep.has_labels || mode === 'modified' && !ep.modified || mode === 'unmodified' && ep.modified) continue;
    count++;
    const button = document.createElement('button'); button.className = 'episode-row'; button.classList.toggle('active', state.ep?.episode_index === ep.episode_index);
    const left = document.createElement('div'), title = document.createElement('div'), detail = document.createElement('div'), badge = document.createElement('span');
    title.className = 'ep-number'; title.textContent = `EP ${String(ep.episode_index).padStart(6, '0')} `;
    const status = document.createElement('span'); status.className = `review-status ${ep.modified ? 'modified' : 'unmodified'}`;
    status.textContent = ep.modified ? '✓ 已修改' : '未修改'; title.append(status);
    detail.className = 'ep-state'; detail.textContent = ep.has_labels ? '已有标签' : '线性初值';
    badge.className = `badge ${categoryClass(ep.category)}`; badge.textContent = category(ep.category);
    left.append(title, detail); button.append(left, badge); button.addEventListener('click', () => loadEpisode(ep.episode_index)); list.append(button);
  }
  if (!count) { const div = document.createElement('div'); div.className = 'list-empty'; div.textContent = '没有符合条件的 episode'; list.append(div); }
}
for (const name of ['episodeSearch', 'categoryFilter', 'labelFilter']) $(name).addEventListener('input', renderEpisodes);
async function loadEpisode(id, force = false) {
  if (state.busy || (!force && !canLeave())) return;
  pause(); state.busy = true; updateControls();
  try {
    const ep = await api(`/api/session/${state.session.session}/episode/${id}`);
    state.generation++; state.ep = ep; acceptEpisode(ep); loadAnchors(); state.chunk = 0; state.action = 0; state.goalTime = ep.timestamps[0]; state.goalMediaTime = ep.timestamps[0] + 0.5 / ep.fps; state.master = null;
    $('emptyState').hidden = true; $('editor').hidden = false;
    $('episodeTitle').textContent = `Episode ${String(id).padStart(6, '0')}`;
    $('episodeMeta').textContent = `${ep.n_steps} actions · ${format(ep.duration, 2)} 秒 · ${ep.tasks.join(' / ')}`;
    $('categoryBadge').className = `badge ${categoryClass(ep.category)}`; $('categoryBadge').textContent = category(ep.category);
    $('sourceBadge').textContent = ep.has_labels ? '已有 value 标签' : '0 → 1 线性初值';
    $('actionSeek').max = ep.n_steps - 1;
    $('curveSeek').min = ep.timestamps[0]; $('curveSeek').max = ep.duration;
    for (let i = 0; i < 3; i++) { videos[i].removeAttribute('src'); videos[i].load(); $(`videoStatus${i}`).textContent = '正在准备可播放视频…'; $(`videoStatus${i}`).classList.remove('hidden'); }
    $('play').disabled = true; $('playChunk').disabled = true;
    renderEpisodes(); renderTable(); selectChunk(0); draw();
    pollVideos(state.generation);
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; updateControls(); }
}
function acceptEpisode(ep) { state.ep = ep; state.points = [...ep.points]; state.flags = [...ep.annotated]; state.saved = [...ep.points]; state.savedFlags = [...ep.annotated]; }
async function pollVideos(generation) {
  if (generation !== state.generation || !state.ep) return;
  try {
    const value = await api(route(`/videos?format=${videoFormat}`)); if (generation !== state.generation) return;
    let pending = false;
    for (const item of value.videos) {
      const v = videos[item.camera], status = $(`videoStatus${item.camera}`);
      if (item.state === 'preparing') pending = true;
      else if (item.state === 'error') status.textContent = item.message;
      else if (!v.hasAttribute('src')) { v.src = item.url; v.playbackRate = Number($('speed').value); v.load(); }
    }
    if (pending) setTimeout(() => pollVideos(generation), 1500);
  } catch (error) { if (generation === state.generation) toast(error.message, true); }
}
for (let i = 0; i < videos.length; i++) {
  videos[i].addEventListener('loadedmetadata', () => {
    const v = videos[i]; v.currentTime = Math.min(state.goalMediaTime, Math.max(0, v.duration - 0.001));
  });
  videos[i].addEventListener('loadeddata', () => {
    if (!state.ep || !videos[i].hasAttribute('src')) return;
    $(`videoStatus${i}`).classList.add('hidden');
    state.master = videos.find(v => v.readyState >= 2 && v.hasAttribute('src')) || null;
    $('play').disabled = !state.master; $('playChunk').disabled = !state.master;
    if (state.playing) videos[i].play().catch(() => {});
  });
  videos[i].addEventListener('error', () => { if (videos[i].hasAttribute('src')) { $(`videoStatus${i}`).textContent = '视频读取失败，请重新载入 episode。'; $(`videoStatus${i}`).classList.remove('hidden'); } });
  videos[i].addEventListener('ended', () => { if (videos[i] === state.master) pause(); });
}
function pause() { state.playbackGeneration++; state.playing = false; state.chunkStop = null; state.chunkPlaying = null; videos.forEach(v => v.pause()); $('play').textContent = '▶'; $('play').setAttribute('aria-label', '播放'); }
async function play() {
  if (!state.master) return;
  const generation = state.playbackGeneration;
  if (state.master.currentTime >= state.ep.duration - 0.04 || state.master.ended) seekAction(0, false);
  // Seeking temporarily lowers readyState to HAVE_METADATA; play() waits for buffering.
  const promises = videos.filter(v => v.readyState >= 1 && v.hasAttribute('src')).map(v => { v.currentTime = Math.min(state.goalMediaTime, v.duration - 0.001); return v.play(); });
  const results = await Promise.allSettled(promises);
  if (generation === state.playbackGeneration && results.some(r => r.status === 'fulfilled')) { state.playing = true; $('play').textContent = 'Ⅱ'; $('play').setAttribute('aria-label', '暂停'); }
}
$('play').addEventListener('click', () => state.playing ? pause() : play());
$('playChunk').addEventListener('click', async () => { const chunk = state.chunk; seekAction(state.ep.chunks[chunk].start_step); state.chunkPlaying = chunk; state.chunkStop = state.ep.chunks[chunk].end_timestamp; await play(); });
$('speed').addEventListener('change', () => videos.forEach(v => v.playbackRate = Number($('speed').value)));
function findAction(time) {
  const ts = state.ep.timestamps; let lo = 0, hi = ts.length;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (ts[mid] <= time + 0.00001) lo = mid + 1; else hi = mid; }
  return Math.max(0, lo - 1);
}
function seekAction(action, stop = true) {
  if (!state.ep) return;
  if (stop) pause();
  action = Math.max(0, Math.min(state.ep.n_steps - 1, action));
  state.goalTime = state.ep.timestamps[action];
  // Seek inside the target frame to avoid float32 and WebM timestamp rounding.
  state.goalMediaTime = state.goalTime + 0.5 / state.ep.fps;
  for (const v of videos) if (v.readyState >= 1) v.currentTime = Math.min(state.goalMediaTime, Math.max(0, v.duration - 0.001));
  updateAction(action); draw();
}
function updateAction(action) {
  state.action = action; $('actionSeek').value = action;
  const time = state.ep.timestamps[action];
  $('curveSeek').value = Math.min(state.ep.duration, state.goalTime);
  $('curveSeekTime').textContent = `${format(state.goalTime, 2)} s`;
  $('actionInfo').textContent = `ACTION ${action} / ${state.ep.n_steps - 1} · 原帧 ${state.ep.frame_indices[action]} · ${format(time, 3)} s`;
  $('timeInfo').textContent = `${format(time, 2)} s`;
  const k = Math.min(state.ep.chunks.length - 1, Math.floor(action / 20));
  if (state.chunk !== k) selectChunk(k, false);
  $('cursorInfo').textContent = `chunk ${k} · action ${action}`;
}
$('actionSeek').addEventListener('input', () => seekAction(Number($('actionSeek').value)));
function beginScrub(source) {
  if (!state.ep || state.busy) return;
  state.scrubbing = {source, resume: state.playing, generation: state.generation}; pause();
}
function scrubToTime(time) { if (state.ep && !state.busy) seekAction(findAction(Math.max(state.ep.timestamps[0], Math.min(state.ep.duration, time))), false); }
function endScrub() {
  const scrub = state.scrubbing; state.scrubbing = null;
  if (scrub?.resume && scrub.generation === state.generation) play();
}
$('curveSeek').addEventListener('pointerdown', () => beginScrub('slider'));
$('curveSeek').addEventListener('input', () => { if (!state.scrubbing) pause(); scrubToTime(Number($('curveSeek').value)); });
$('curveSeek').addEventListener('pointerup', endScrub);
$('curveSeek').addEventListener('pointercancel', endScrub);
document.addEventListener('pointerup', () => { if (state.scrubbing?.source === 'slider') endScrub(); });
function tick() {
  if (state.playing && state.master && state.ep) {
    let time = state.master.currentTime; state.goalTime = time; state.goalMediaTime = time;
    if (state.chunkStop !== null && time >= state.chunkStop - 0.004) {
      const lastAction = state.ep.chunks[state.chunkPlaying ?? state.chunk].end_step_exclusive - 1;
      pause(); seekAction(lastAction, false); time = state.goalTime;
    }
    else for (const v of videos) if (v !== state.master && v.readyState >= 2 && Math.abs(v.currentTime - time) > 1 / state.ep.fps) v.currentTime = Math.min(time, v.duration - 0.001);
    updateAction(findAction(time)); draw();
  }
  requestAnimationFrame(tick);
}
function selectChunk(chunk, seek = true) {
  if (!state.ep) return;
  state.chunk = Math.max(0, Math.min(state.ep.chunks.length - 1, chunk));
  state.selectedPoint = state.chunk + 1;
  if (seek) {
    const c = state.ep.chunks[state.chunk];
    seekAction(state.chunkView === 'end' ? c.end_step_exclusive - 1 : c.start_step);
  }
  renderChunkEditor(); updateTableSelection(); draw();
}
function renderChunkEditor() {
  const k = state.chunk, c = state.ep.chunks[k];
  $('chunkTitle').textContent = `CHUNK ${String(k).padStart(2, '0')} / ${state.ep.chunks.length - 1}`;
  $('chunkRange').textContent = `action ${c.start_step}–${c.end_step_exclusive - 1} · ${c.valid_action_count} actions`;
  if (document.activeElement !== $('progressInput')) $('progressInput').value = Number(state.points[k + 1].toFixed(8));
  if (document.activeElement !== $('deltaInput')) $('deltaInput').value = Number((state.points[k + 1] - state.points[k]).toFixed(8));
  $('totalProgress').textContent = format(state.points[state.points.length - 1], 6);
  $('startProgress').textContent = format(state.points[k]);
  $('chunkDelta').textContent = format(state.points[k + 1] - state.points[k]);
  $('originalProgress').textContent = format(state.ep.baseline_points[k + 1]);
  $('prevChunk').disabled = k === 0; $('nextChunk').disabled = k === state.ep.chunks.length - 1;
  $('viewStart').setAttribute('aria-pressed', state.chunkView === 'start');
  $('viewEnd').setAttribute('aria-pressed', state.chunkView === 'end');
  renderAnchorControls();
}
function defaultAnchors() { return new Set([0, state.points.length - 1]); }
function anchorKey() { return `value-studio:anchors:v2:${JSON.stringify([state.session.label_path, state.ep.episode_index])}`; }
function loadAnchors() {
  const key = anchorKey(); let values = state.anchorCache.get(key);
  try { const stored = JSON.parse(localStorage.getItem(key)); if (Array.isArray(stored)) values = stored; } catch { /* Browser storage is optional. */ }
  if (!Array.isArray(values)) {
    values = [...defaultAnchors()];
    // Migrate existing selections once, adding the new default endpoint anchors.
    try { const previous = JSON.parse(localStorage.getItem(key.replace(':anchors:v2:', ':anchors:'))); if (Array.isArray(previous)) values.push(...previous); } catch { /* Use defaults if storage is unavailable. */ }
  }
  state.anchors = new Set(values.filter(p => Number.isInteger(p) && p >= 0 && p < state.points.length));
  persistAnchors();
}
function persistAnchors() {
  const values = [...state.anchors].sort((a, b) => a - b), key = anchorKey(); state.anchorCache.set(key, values);
  try { localStorage.setItem(key, JSON.stringify(values)); } catch { /* Retain anchors in memory when storage is unavailable. */ }
}
function renderAnchorControls() {
  const anchored = state.anchors.has(state.selectedPoint);
  $('toggleAnchor').textContent = anchored ? '取消线性锚定' : '线性锚定';
  $('toggleAnchor').setAttribute('aria-pressed', anchored);
  $('toggleAnchor').disabled = !state.ep || state.busy;
  $('interpolate').disabled = !state.ep || state.busy || state.anchors.size < 2;
  const point = state.selectedPoint === 0 ? 'episode 起点' : `chunk ${String(state.selectedPoint - 1).padStart(2, '0')} 结束点`;
  $('anchorSummary').textContent = `${point} · 已锚定 ${state.anchors.size} 点`;
  $('anchorSummary').title = `${point}；右键曲线点可锚定或取消；仅插值相邻锚点之间，锚点外保持不变`;
}
function toggleAnchor(point = state.selectedPoint) {
  if (!state.ep || state.busy) return;
  state.anchors.has(point) ? state.anchors.delete(point) : state.anchors.add(point);
  persistAnchors(); renderAnchorControls(); draw();
}
function interpolateAnchors() {
  if (!state.ep || state.busy || state.anchors.size < 2) return;
  const anchors = [...state.anchors].sort((a, b) => a - b), times = state.ep.boundary_times, next = [...state.points];
  for (let n = 1; n < anchors.length; n++) {
    const left = anchors[n - 1], right = anchors[n], duration = times[right] - times[left];
    if (!(duration > 0)) { toast('锚点时间必须递增，无法对这些点进行线性插值。', true); return; }
    for (let i = left + 1; i < right; i++) {
      const ratio = (times[i] - times[left]) / duration;
      next[i] = (1 - ratio) * state.points[left] + ratio * state.points[right];
    }
  }
  if (next.some(v => !Number.isFinite(v))) { toast('插值结果超出数值范围。', true); return; }
  const changed = next.filter((v, i) => v !== state.points[i]).length;
  pause(); state.points = next;
  state.flags = state.savedFlags.map((flag, k) => flag || next[k] !== state.saved[k] || next[k + 1] !== state.saved[k + 1]);
  renderChunkEditor(); renderTable(); draw(); updateControls();
  toast(changed ? `已线性插值 ${changed} 个点；点击保存写入标签` : '锚点之间的曲线已是线性，无需修改');
}
$('toggleAnchor').addEventListener('click', () => toggleAnchor());
$('interpolate').addEventListener('click', interpolateAnchors);
function setProgress(point, value, updateInput = true) {
  if (state.busy || point === 0 || !Number.isFinite(value)) return;
  state.points[point] = value;
  state.flags[point - 1] = value !== state.saved[point] || state.savedFlags[point - 1];
  renderChunkEditor(); if (updateInput) $('progressInput').value = Number(value.toFixed(8));
  renderTable(); draw(); updateControls();
}
$('progressInput').addEventListener('input', () => { const input = $('progressInput'); if (input.value !== '' && input.validity.valid) setProgress(state.chunk + 1, input.valueAsNumber, false); });
$('progressInput').addEventListener('focus', pause);
$('deltaInput').addEventListener('focus', pause);
$('progressInput').addEventListener('blur', () => { if (state.ep) $('progressInput').value = Number(state.points[state.chunk + 1].toFixed(8)); });
function setDelta(value) {
  if (state.busy || !Number.isFinite(value)) return;
  const k = state.chunk, diff = value - (state.points[k + 1] - state.points[k]);
  const next = state.points.map((p, i) => i > k ? p + diff : p);
  if (next.some(p => !Number.isFinite(p))) return;
  state.points = next;
  for (let i = k; i < state.flags.length; i++) state.flags[i] = state.points[i + 1] !== state.saved[i + 1] || state.savedFlags[i];
  renderChunkEditor(); renderTable(); draw(); updateControls();
}
$('deltaInput').addEventListener('input', () => { const input = $('deltaInput'); if (input.value !== '' && input.validity.valid) setDelta(input.valueAsNumber); });
$('deltaInput').addEventListener('blur', () => { if (state.ep) $('deltaInput').value = Number((state.points[state.chunk + 1] - state.points[state.chunk]).toFixed(8)); });
$('resetPoint').addEventListener('click', () => setProgress(state.chunk + 1, state.ep.baseline_points[state.chunk + 1]));
$('prevChunk').addEventListener('click', () => selectChunk(state.chunk - 1)); $('nextChunk').addEventListener('click', () => selectChunk(state.chunk + 1));
function setChunkView(view) {
  if (!state.ep || state.busy) return;
  state.chunkView = view;
  selectChunk(state.chunk);
}
$('viewStart').addEventListener('click', () => setChunkView('start'));
$('viewEnd').addEventListener('click', () => setChunkView('end'));
function renderTable() {
  if (!state.ep) return;
  const tbody = $('chunkTable'); tbody.replaceChildren(); $('tableCount').textContent = `${state.ep.chunks.length} chunks`;
  for (const c of state.ep.chunks) {
    const k = c.chunk_index, row = document.createElement('tr'); row.dataset.chunk = k; row.classList.toggle('selected', k === state.chunk);
    const values = [String(k).padStart(2, '0'), `${c.start_step}–${c.end_step_exclusive - 1}`, `${format(c.start_timestamp, 2)}–${format(c.end_timestamp, 2)}`, format(state.points[k]), format(state.points[k + 1]), format(state.points[k + 1] - state.points[k]), state.points[k + 1] !== state.saved[k + 1] ? '待保存' : state.flags[k] ? '人工确认' : state.ep.has_labels ? '已有标签' : '线性初值'];
    values.forEach((v, i) => { const cell = document.createElement('td'); cell.textContent = v; if ((i === 4 || i === 6) && state.points[k + 1] !== state.saved[k + 1]) cell.className = 'changed-value'; row.append(cell); });
    row.addEventListener('click', () => selectChunk(k)); tbody.append(row);
  }
}
function updateTableSelection() { for (const row of $('chunkTable').children) row.classList.toggle('selected', Number(row.dataset.chunk) === state.chunk); }
const cv = $('curve'), ctx = cv.getContext('2d');
function axes() {
  const w = cv.clientWidth, h = cv.clientHeight, m = {l: 48, r: 18, t: 18, b: 27};
  const vals = [0, 1, ...state.points, ...state.ep.baseline_points];
  const min = Math.min(...vals), max = Math.max(...vals), pad = (max - min) * 0.08;
  const low = state.drag?.low ?? min - pad, high = state.drag?.high ?? max + pad;
  const start = state.ep.boundary_times[0], end = state.ep.duration;
  return {w, h, m, low, high, x: t => m.l + (t - start) / (end - start) * (w - m.l - m.r),
    y: v => m.t + (high - v) / (high - low) * (h - m.t - m.b),
    value: py => high - (py - m.t) / (h - m.t - m.b) * (high - low),
    time: px => start + (px - m.l) / (w - m.l - m.r) * (end - start)};
}
function draw() {
  if (!state.ep || cv.clientWidth < 20) return;
  const dpr = window.devicePixelRatio || 1, a = axes();
  if (cv.width !== Math.round(a.w * dpr) || cv.height !== Math.round(a.h * dpr)) { cv.width = Math.round(a.w * dpr); cv.height = Math.round(a.h * dpr); }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, a.w, a.h);
  ctx.font = '10px ui-monospace, monospace'; const k = state.chunk, bounds = state.ep.boundary_times;
  ctx.fillStyle = '#e8f4f0'; ctx.fillRect(a.x(bounds[k]), a.m.t, a.x(bounds[k + 1]) - a.x(bounds[k]), a.h - a.m.t - a.m.b);
  for (let i = 0; i <= 4; i++) {
    const value = i / 4, y = a.y(value); ctx.strokeStyle = '#edf0f2'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(a.m.l, y); ctx.lineTo(a.w - a.m.r, y); ctx.stroke(); ctx.fillStyle = '#95a0a6'; ctx.textAlign = 'right'; ctx.fillText(format(value, 2), a.m.l - 9, y + 3);
  }
  ctx.textAlign = 'center';
  for (let i = 0; i <= 5; i++) { const t = bounds[0] + (state.ep.duration - bounds[0]) * i / 5; ctx.fillStyle = '#95a0a6'; ctx.fillText(format(t, 1), a.x(t), a.h - 8); }
  const line = (values, color, dash) => { ctx.strokeStyle = color; ctx.lineWidth = dash.length ? 1.5 : 2; ctx.setLineDash(dash); ctx.beginPath(); values.forEach((v, i) => i ? ctx.lineTo(a.x(bounds[i]), a.y(v)) : ctx.moveTo(a.x(bounds[i]), a.y(v))); ctx.stroke(); ctx.setLineDash([]); };
  line(state.ep.baseline_points, '#bba076', [4, 4]); line(state.points, '#127f76', []);
  for (let i = 0; i < state.points.length; i++) {
    const selected = i === state.selectedPoint, anchored = state.anchors.has(i), x = a.x(bounds[i]), y = a.y(state.points[i]);
    ctx.beginPath(); ctx.arc(x, y, selected ? 5.5 : 3, 0, Math.PI * 2); ctx.fillStyle = i === 0 ? '#95aaa1' : selected ? '#127f76' : '#fff'; ctx.fill(); ctx.strokeStyle = '#127f76'; ctx.lineWidth = selected ? 2 : 1.3; ctx.stroke();
    if (anchored) { ctx.strokeStyle = '#b98027'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, y - 8); ctx.lineTo(x + 8, y); ctx.lineTo(x, y + 8); ctx.lineTo(x - 8, y); ctx.closePath(); ctx.stroke(); }
  }
  const cursor = a.x(Math.min(state.goalTime, state.ep.duration)); ctx.strokeStyle = '#567b7566'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(cursor, a.m.t); ctx.lineTo(cursor, a.h - a.m.b); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = '#8b9d95'; ctx.textAlign = 'left'; ctx.fillText('PROGRESS', a.m.l, 10);
}
function hitCurvePoint(x, y, a) {
  let point = -1, distance = 16;
  for (let i = 0; i < state.points.length; i++) { const d = Math.hypot(a.x(state.ep.boundary_times[i]) - x, a.y(state.points[i]) - y); if (d < distance) { distance = d; point = i; } }
  return point;
}
cv.addEventListener('contextmenu', event => {
  if (!state.ep || state.busy) return;
  const rect = cv.getBoundingClientRect(), point = hitCurvePoint(event.clientX - rect.left, event.clientY - rect.top, axes());
  if (point < 0) return;
  event.preventDefault(); selectChunk(Math.max(0, point - 1), false);
  seekAction(point === 0 ? 0 : state.ep.chunks[point - 1].end_step_exclusive - 1);
  state.selectedPoint = point; toggleAnchor(point);
});
cv.addEventListener('pointerdown', event => {
  if (!state.ep || state.busy || event.button !== 0) return;
  const rect = cv.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top, a = axes();
  const point = hitCurvePoint(x, y, a);
  if (point > 0) { selectChunk(point - 1, false); seekAction(state.ep.chunks[point - 1].end_step_exclusive - 1); state.drag = {point, low: a.low, high: a.high, startY: y, moved: false}; cv.setPointerCapture(event.pointerId); }
  else if (point === 0) { selectChunk(0, false); seekAction(0); state.selectedPoint = 0; renderAnchorControls(); draw(); }
  else { beginScrub('chart'); scrubToTime(a.time(x)); cv.setPointerCapture(event.pointerId); }
});
cv.addEventListener('pointermove', event => {
  if (!state.ep) return;
  const rect = cv.getBoundingClientRect(), a = axes(), x = event.clientX - rect.left, y = event.clientY - rect.top;
  if (state.drag) { if (Math.abs(y - state.drag.startY) > 2) state.drag.moved = true; if (state.drag.moved) setProgress(state.drag.point, Number(a.value(Math.max(a.m.t, Math.min(a.h - a.m.b, y))).toFixed(6))); }
  else if (state.scrubbing?.source === 'chart') scrubToTime(a.time(x));
  else cv.style.cursor = state.points.some((v, i) => i > 0 && Math.hypot(a.x(state.ep.boundary_times[i]) - x, a.y(v) - y) < 14) ? 'ns-resize' : 'ew-resize';
});
function endDrag() { if (state.drag) { state.drag = null; draw(); } if (state.scrubbing?.source === 'chart') endScrub(); }
cv.addEventListener('pointerup', endDrag); cv.addEventListener('pointercancel', endDrag);
new ResizeObserver(draw).observe(cv);
$('save').addEventListener('click', save);
async function refreshEpisodes() { const value = await api(`/api/session/${state.session.session}/episodes`); state.episodes = value.episodes; renderEpisodes(); }
async function save() {
  if (!state.ep || state.busy) return;
  pause(); state.busy = true; updateControls();
  try {
    const result = await api(route('/save'), {points: state.points, annotated: state.flags, revision: state.ep.revision});
    acceptEpisode(result); $('sourceBadge').textContent = '已有 value 标签'; renderTable(); renderChunkEditor(); draw();
    toast('已保存，此轨迹标记为已修改'); await refreshEpisodes();
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; updateControls(); }
}
$('restore').addEventListener('click', () => { if (state.ep && !state.busy) $('restoreDialog').showModal(); });
$('resetEpisode').addEventListener('click', () => { if (state.ep && !state.busy) $('restoreDialog').showModal(); });
$('cancelRestore').addEventListener('click', () => $('restoreDialog').close());
$('confirmRestore').addEventListener('click', async () => {
  if (state.busy) return;
  pause(); state.busy = true; updateControls();
  try {
    const result = await api(route('/restore'), {revision: state.ep.revision}); acceptEpisode(result);
    state.anchors = defaultAnchors(); persistAnchors();
    $('restoreDialog').close(); renderTable(); renderChunkEditor(); draw();
    $('sourceBadge').textContent = result.has_labels ? '已有 value 标签' : '0 → 1 线性初值';
    toast('已恢复整条初值，此轨迹标记为未修改'); await refreshEpisodes();
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; updateControls(); }
});
window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); return; }
  if (!state.ep || state.busy || ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName) || $('restoreDialog').open) return;
  if (event.key === 'ArrowLeft') { event.preventDefault(); selectChunk(state.chunk - 1); }
  if (event.key === 'ArrowRight') { event.preventDefault(); selectChunk(state.chunk + 1); }
  if (event.code === 'Space') { event.preventDefault(); state.playing ? pause() : play(); }
});
init(); requestAnimationFrame(tick);
