
let currentMode = 'video';
let currentInfo = null;
let currentPlaylist = null;
let pendingTaskIds = [];
let pendingTaskId = null;
let hasPreparedFile = false;
let tagCands = [];
let serverFormats = null;
let selCand = -1;
let selArt = {type:'video', url:''};
let retagTaskId = null;
let lastReadyData = null;
let progressListeners = {};
let busyDownload = false;
let prepareFailures = [];
let prepareCancelled = false;
let preparedResults = [];
const $ = (id)=>document.getElementById(id);

/* ---------- Step flow (one screen at a time) ---------- */
function setScreen(s){
  ['screenImport','screenConfigure','screenTags','screenResult'].forEach(id=>{
    document.getElementById(id).classList.toggle('hidden', id!==s);
  });
  $('screenNav').classList.toggle('hidden', s==='screenImport');
  if(s !== 'screenResult'){
    const v = $('previewVideo'), a = $('previewAudio');
    if(v) v.pause();
    if(a) a.pause();
  }
}
function toggleQuality(){
  const t = $('qualityToggle');
  if(t.disabled) return;
  const open = t.getAttribute('aria-expanded') === 'true';
  t.setAttribute('aria-expanded', String(!open));
  $('qlistWrap').classList.toggle('hidden', open);
}
function qualityLabel(){
  const n = $('formatList').querySelectorAll('.qcard').length;
  if(!n){ $('qualityToggleLabel').textContent = 'No formats available'; return; }
  const cards = $('formatList').querySelectorAll('.qcard.active');
  const pick = cards.length
    ? cards[0].querySelector('.q-main').textContent.trim()
    : $('formatList').querySelector('.qcard').querySelector('.q-main').textContent.trim();
  $('qualityToggleLabel').textContent = 'Quality · ' + pick + ' · ' + n + ' options';
}
function goImport(){
  if(busyDownload) return;
  setScreen('screenImport');
  const u = $('url');
  u.focus();
  u.select();
}
function goBack(){
  const cur = ['screenImport','screenConfigure','screenTags','screenResult'].find(id=>!document.getElementById(id).classList.contains('hidden'));
  if(cur==='screenConfigure'){ goImport(); }
  else if(cur==='screenTags'){ tagsCancel(); }
  else { setScreen('screenConfigure'); }
}

/* ---------- Session (per-browser identity for isolation) ---------- */
function getSession(){
  let s = localStorage.getItem('md_session');
  if(!s){
    s = (crypto.randomUUID && crypto.randomUUID()) ||
        ('s'+Math.random().toString(36).slice(2)+Date.now().toString(36));
    localStorage.setItem('md_session', s);
  }
  return s;
}

/* ---------- Optional auth (server MD_TOKEN) ---------- */
function authToken(){
  let t = null;
  try{ t = sessionStorage.getItem('md_token'); }catch(_){}
  return t;
}
function authHeaders(){
  const t = authToken();
  return t ? {'Authorization':'Bearer '+t} : {};
}
function authQuery(alreadyHasParams){
  const t = authToken();
  return t ? (alreadyHasParams ? '&' : '?') + 'token=' + encodeURIComponent(t) : '';
}
async function apiFetch(path, opts){
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {}, authHeaders());
  let res = await fetch(path, Object.assign({}, opts, {headers}));
  if(res.status === 401 && !authToken()){
    const t = window.prompt('This server is password-protected. Enter its access token:', '');
    if(t){
      try{ sessionStorage.setItem('md_token', t); }catch(_){}
      return apiFetch(path, opts);
    }
  }
  return res;
}

/* ---------- Format lists (server-driven so the UI matches capabilities) ---------- */
(async function initFormats(){
  try{
    const res = await apiFetch('/api/formats');
    const d = await res.json();
    if(!res.ok) throw new Error(Object(d).error || ('formats '+res.status));
    serverFormats = d;
  }catch(_){
    serverFormats = null;
  }
})();

/* ---------- Theme ---------- */
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const icon = t === 'dark'
    ? '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
    : '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>';
  $('themeToggle').innerHTML = icon;
  try{ localStorage.setItem('md_theme', t); }catch(_){}
}
(function initTheme(){
  let t = null;
  try{ t = localStorage.getItem('md_theme'); }catch(_){}
  if(t !== 'dark' && t !== 'light'){
    t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  }
  applyTheme(t);
})();
$('themeToggle').addEventListener('click', ()=>{
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
});

/* ---------- Feedback ---------- */
function showFeedback(html, kind){
  const fb = $('feedback');
  fb.innerHTML = '';
  if(!html){ return; }
  const d = document.createElement('div');
  d.className = 'alert alert-' + (kind || 'info');
  d.innerHTML = html;
  fb.appendChild(d);
}

/* ---------- Paste (fills box only, no fetch) ---------- */
async function pasteUrl(){
  const input = $('url');
  const btn = $('pasteBtn');
  showFeedback('');
  btn.disabled = true;
  btn.textContent = 'Pasting…';
  try{
    let text = '';
    if(navigator.clipboard && navigator.clipboard.readText){
      text = await navigator.clipboard.readText();
    }
    if(!text){ throw new Error('Clipboard is empty or inaccessible'); }
    input.value = text.trim();
  }catch(e){
    showFeedback('Could not paste: '+((e&&e.message)||e), 'error');
  }finally{
    btn.disabled = false;
    btn.textContent = 'Paste';
  }
}

/* ---------- Fetch ---------- */
function setFetching(on){
  $('fetchBtn').disabled = on;
  $('fetchBtn').innerHTML = on ? '<span class="spinner"></span> Loading' : 'Fetch info';
  $('fetchBtn').setAttribute('aria-busy', on ? 'true' : 'false');
  $('url').disabled = on;
}
async function fetchInfo(){
  const url = $('url').value.trim();
  if(!url){ showFeedback('Please paste a link first.', 'error'); return; }
  if(busyDownload){ showFeedback('A download is in progress — finish or clear it before fetching another video.', 'error'); return; }
  setFetching(true);
  showFeedback('<div class="progress-wrap" style="width:100%;margin-top:0"><div class="progress-track"><div class="progress-fill indeterminate"></div></div><div class="progress-text">Fetching video info…</div></div>');
  const modeParam = currentMode === 'audio' ? 'audio' : 'video';
  try{
    const res = await apiFetch('/api/info?url='+encodeURIComponent(url)+'&mode='+modeParam);
    const data = await res.json();
    if(!res.ok || data.error){ throw new Error(data.error || ('Request failed ('+res.status+')')); }
    showFeedback('');
    if(data._is_playlist){ showPlaylist(url, data); }
    else{ showInfo(data); }
  }catch(e){
    showFeedback('Could not fetch info: '+((e&&e.message)||e), 'error');
  }finally{
    setFetching(false);
  }
}

/* The candidate lookup happens server-side at prepare time; the picker lives on
   its own optional step (screenTags), so there is nothing to pre-fetch here. */
function updateTagCard(){
  $('tagCard').classList.add('hidden');
}

function showInfo(info){
  currentInfo = info;
  const u = $('url').value.trim();
  $('urlPill').textContent = u;
  $('urlPill').title = u;
  setScreen('screenConfigure');
  $('infoCard').classList.remove('hidden');
  $('prepareBtn').classList.remove('hidden');
  $('playlistCard').classList.add('hidden');
  $('thumb').src = info.thumbnail || '';
  $('title').textContent = info.title || 'Untitled';
  const parts = [];
  if(info.uploader) parts.push(info.uploader);
  if(info.duration_string) parts.push(info.duration_string);
  if(info.view_count) parts.push(info.view_count.toLocaleString()+' views');
  $('metaLine').textContent = parts.join(' · ');
  $('progressWrap').classList.add('hidden');
  $('resultArea').classList.add('hidden');
  populateFormats();
  updateTagCard();
}

function formatSize(b){
  if(!b) return '';
  if(b>1e9) return (b/1e9).toFixed(1)+'GB';
  if(b>1e6) return (b/1e6).toFixed(0)+'MB';
  if(b>1e3) return (b/1e3).toFixed(0)+'KB';
  return b+'B';
}

/* ---------- Quality cards ---------- */
function showEmpty(label){
  const list = $('formatList');
  list.innerHTML = '<div class="empty-note">'+label+'</div>';
}
function addQCard(list, value, main, sub){
  const b = document.createElement('button');
  b.className = 'qcard';
  b.dataset.value = value;
  b.innerHTML = '<span class="q-check"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 13 4.5 4.5L19 7"/></svg></span><div class="q-main"></div><div class="q-sub"></div>';
  b.querySelector('.q-main').textContent = main;
  b.querySelector('.q-sub').textContent = sub || '';
  b.addEventListener('click', ()=>{
    selectCard(b, value);
    $('qualityToggle').setAttribute('aria-expanded','false');
    $('qlistWrap').classList.add('hidden');
    qualityLabel();
  });
  list.appendChild(b);
}
function selectCard(el, value){
  document.querySelectorAll('#formatList .qcard').forEach(c=>c.classList.toggle('active', c===el));
  $('formatList').dataset.selected = value;
  qualityLabel();
}

function populateFormats(){
  const list = $('formatList');
  $('modeVideo').classList.toggle('active', currentMode==='video');
  $('modeAudio').classList.toggle('active', currentMode==='audio');
  list.innerHTML = '';

  if(currentMode === 'video'){
    const vids = (currentInfo && currentInfo.video_formats) || [];
    const isPlaylist = !!currentPlaylist;
    const needFfmpeg = currentInfo ? currentInfo._ffmpeg !== true : true;
    if(vids.length || isPlaylist){
      addQCard(list, 'MP4 (best)', 'MP4 (best)', needFfmpeg ? 'Auto best quality · merging needs ffmpeg' : 'Auto best quality');
      if(needFfmpeg){
        const first = list.querySelector('.qcard');
        if(first) first.classList.add('needs-ffmpeg');
      }
      const seen = new Set();
      for(const f of vids){
        if(!f.height) continue;
        const h = f.height;
        if(seen.has(h)) continue;
        seen.add(h);
        let sub = (f.ext||'mp4').toUpperCase() + (f.fps ? ' · '+f.fps+'fps' : '');
        if(f.filesize) sub += ' · '+formatSize(f.filesize);
        addQCard(list, 'MP4 '+h+'p', 'MP4 '+h+'p', sub);
      }
      const qcards = list.querySelectorAll('.qcard');
      if(needFfmpeg && qcards[1]){
        selectCard(qcards[1], qcards[1].dataset.value);
      }else if(qcards[0]){
        selectCard(qcards[0], qcards[0].dataset.value);
      }
    }else{
      showEmpty('No downloadable video');
    }
  }else{
    const serverAudio = (serverFormats && serverFormats.audio_formats) || [];
    if(serverAudio.length){
      for(const entry of serverAudio){
        const label = entry[1];
        const convert = (entry.length >= 4 && entry[3]) ? entry[3] : null;
        let sub;
        if(convert){
          sub = 'Converted'
            + (convert.abr ? ' · '+convert.abr+'kbps' : '')
            + ((convert.out_ext || convert.acodec) ? ' · '+String(convert.out_ext||convert.acodec).toUpperCase() : '');
        }else{
          sub = 'Best available quality';
        }
        addQCard(list, label, label, sub);
      }
      const prev = $('formatList').dataset.selected;
      let defaultLabel = null;
      if(prev && serverAudio.some(e=>e[1]===prev)) defaultLabel = prev;
      if(!defaultLabel){
        if(!serverAudio.some(e=>e[1]==='MP3 320kbps')) defaultLabel = 'MP3 128kbps';
        else defaultLabel = 'MP3 320kbps';
        if(!serverAudio.some(e=>e[1]===defaultLabel)) defaultLabel = serverAudio[0][1];
      }
      let el = null;
      list.querySelectorAll('.qcard').forEach(c=>{ if(c.dataset.value===defaultLabel) el = c; });
      selectCard(el || list.querySelector('.qcard'), defaultLabel);
    }else{
      showEmpty('No downloadable audio');
    }
  }
  $('qualityToggle').disabled = $('formatList').querySelectorAll('.qcard').length === 0;
  qualityLabel();
}

function setMode(mode){
  currentMode = mode;
  populateFormats();
  updateTagCard();
  updatePlaylistModeUI();
}

function playlistModeInfo(){
  if(currentMode === 'audio'){
    const sel = $('formatList').dataset.selected;
    return {mode:'audio', format: sel || 'MP3 320kbps', badge: sel || 'MP3'};
  }
  return {mode:'video', format:'best mp4', badge:'MP4'};
}

function updatePlaylistModeUI(){
  if(!currentPlaylist || !$('playlistCard') || $('playlistCard').classList.contains('hidden')) return;
  const info = playlistModeInfo();
  const list = $('playlistList');
  list.querySelectorAll('.badge').forEach(b => b.textContent = info.badge);
  $('prepareAllBtn').innerHTML = 'Prepare All (' + info.badge + ')';
}

/* ---------- Prepare ---------- */
function setBusy(on){
  busyDownload = on;
  ['fetchBtn','pasteBtn','modeVideo','modeAudio','changeUrlBtn','fetchAnotherBtn'].forEach(id=>{ $(id).disabled = on; });
  document.querySelectorAll('#formatList .qcard').forEach(b=>{ b.disabled = on; });
}
function setPrepareBtn(on, txt){
  $('prepareBtn').disabled = on;
  $('prepareBtn').innerHTML = on ? '<span class="spinner"></span> '+txt : 'Prepare download';
  $('prepareBtn').setAttribute('aria-busy', on ? 'true' : 'false');
  if(on) $('backBtn').classList.add('hidden');
  setBusy(on);
}
async function prepareVideo(){
  const url = $('url').value.trim();
  if(!url) return;
  const sel = $('formatList').dataset.selected;
  const firstQ = $('formatList').querySelector('.qcard');
  const firstVal = firstQ ? firstQ.dataset.value : '';
  const format = sel || (currentMode==='video' ? (firstVal || 'best') : 'MP3 320kbps');
  setPrepareBtn(true, 'Preparing');
  const info = currentInfo || {};
  $('dlThumb').src = info.thumbnail || '';
  $('dlTitle').textContent = info.title || 'Untitled';
  const ctxParts = [];
  if(info.uploader) ctxParts.push(info.uploader);
  if(info.duration_string) ctxParts.push(info.duration_string);
  ctxParts.push((currentMode==='video' ? 'Video' : 'Audio') + ' · ' + format);
  $('dlSub').textContent = ctxParts.join(' · ');
  $('dlContext').classList.remove('hidden');
  $('progressWrap').classList.remove('hidden');
  $('progressFill').closest('.progress-track').classList.remove('hidden');
  $('progressText').classList.remove('hidden');
  $('resultArt').classList.add('hidden');
  const fill = $('progressFill');
  fill.className = 'progress-fill indeterminate';
  const ptext = $('progressText');
  ptext.className = 'progress-text';
  ptext.textContent = 'Preparing…';
  $('resultArea').classList.add('hidden');
  hasPreparedFile = false;
  prepareFailures = [];

  try{
    const res = await apiFetch('/api/prepare', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({
        url, mode: currentMode, format, session: getSession(),
        tags: currentMode === 'audio',
        title: (currentInfo && currentInfo.title) || '',
        creator: (currentInfo && (currentInfo.creator || currentInfo.uploader)) || '',
      }),
    });
    const data = await res.json();
    if(!res.ok || data.error){
      showFeedback(data.error || ('Request failed ('+res.status+')'), 'error');
      setPrepareBtn(false, 'Prepare download');
      return;
    }
    pendingTaskIds = [data.task_id];
    pendingTaskId = data.task_id;
    retagTaskId = null;
    setScreen('screenResult');
    listenProgress(data.task_id);
  }catch(e){
    showFeedback('Network error: '+((e&&e.message)||e), 'error');
    setPrepareBtn(false, 'Prepare download');
  }
}

function matchInCands(cands, ot){
  const keyA = (ot.artist||'').toLowerCase();
  const keyT = (ot.track||'').toLowerCase();
  if(!keyA && !keyT) return 0;
  for(let i=0;i<cands.length;i++){
    const ct = (cands[i].tags)||{};
    if((ct.artist||'').toLowerCase()===keyA && (ct.track||'').toLowerCase()===keyT) return i;
  }
  return 0;
}

async function startWithSelection(taskId, fresh){
  const old = tagCands[selCand] || {};
  selCand = matchInCands(fresh, old.tags || {});
  tagCands = fresh;
  paintCands(tagCands);
  const cur = tagCands[selCand] || {};
  if(selArt.type === 'album'){
    selArt = cur.artwork ? {type:'album', url:cur.artwork} : {type:'video', url:''};
  }else if(selArt.type === 'none'){
    selArt = {type:'none', url:''};
  }else{
    selArt = {type:'video', url:''};
  }
  syncArtSeg();

  const body = {mode:'candidate', tags:cur.tags || {}, art: selArt};
  return retagTaskId ? await applyRetag(taskId, body) : await postAndListen(taskId, body);
}

async function postAndListen(taskId, body){
  try{
    const res = await apiFetch('/api/task/'+taskId+'/tags?session='+encodeURIComponent(getSession()), {
      method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if(!res.ok || data.error){
      throw new Error(data.error || ('Request failed ('+res.status+')'));
    }
  }catch(e){
    showFeedback('Could not start: '+((e&&e.message)||e), 'error');
    setPrepareBtn(false, 'Prepare download');
    return false;
  }
  $('tagCard').classList.add('hidden');
  setScreen('screenResult');
  if(!progressListeners[taskId]) listenProgress(taskId);
  return true;
}

/* ---------- Retag a finished file (after-download tag editor) ---------- */
async function applyRetag(taskId, body){
  try{
    const res = await apiFetch('/api/task/'+taskId+'/retag?session='+encodeURIComponent(getSession()), {
      method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if(!res.ok || data.error){
      throw new Error(data.error || ('Request failed ('+res.status+')'));
    }
    const d = data || {};
    closeTagPicker();
    if(currentPlaylist){
      // Playlist result list: update the row's ready payload and re-render.
      const row = preparedResults.find(r => r.taskId === taskId);
      if(row && row.ready){
        row.ready.tags = d.tags || {};
        row.ready.tags_mode = d.tags_mode || 'candidate';
        if(d.artwork) row.ready.artwork = d.artwork;
        if(d.cover) row.ready.cover = d.cover;
      }
      setScreen('screenConfigure');
      renderPlaylistResult();
      showFeedback('Tags applied to #' + (row ? row.index : taskId) + '.');
      return true;
    }
    setScreen('screenResult');
    if(lastReadyData){
      lastReadyData.tags = d.tags || {};
      lastReadyData.tags_mode = d.tags_mode || 'candidate';
      if(d.artwork) lastReadyData.artwork = d.artwork;
      if(d.cover) lastReadyData.cover = d.cover;
      showReady(lastReadyData);
    }
    showFeedback('Tags applied to the file.');
    return true;
  }catch(e){
    showFeedback('Could not apply tags: '+((e&&e.message)||e), 'error');
    return false;
  }
}
function refreshResultAfterTags(){
  closeTagPicker();
  if(currentPlaylist){
    setScreen('screenConfigure');
    renderPlaylistResult();
    return;
  }
  setScreen('screenResult');
  if(lastReadyData) showReady(lastReadyData);
}

/* ---------- Optional tags & art step (audio with ffmpeg) ---------- */
function setTagsBusy(on, applying){
  ['tagStartBtn','tagSkipBtn','tagCancelBtn','manualSubmitBtn','tabMatch','tabManual','tagSearch','tagSearchBtn'].forEach(id=>{ $(id).disabled = on; });
  document.querySelectorAll('#manualArtSeg button').forEach(b=>{ b.disabled = on; });
  $('coverFile').disabled = on;
  $('tagCard').setAttribute('aria-busy', on ? 'true' : 'false');
  $('tagSearchBtn').innerHTML = on ? '<span class="spinner"></span> Searching' : 'Search';
  if(on && applying){
    const busy = '<span class="spinner"></span> '+(retagTaskId ? 'Applying tags…' : 'Starting…');
    $('tagStartBtn').innerHTML = busy;
    $('manualSubmitBtn').innerHTML = busy;
  }
}
function enterTags(taskId, cands, retag, query){
  pendingTaskId = taskId;
  retagTaskId = retag ? taskId : null;
  $('tagSearch').value = query || '';
  $('tagSearch').disabled = false;
  $('tagSearchBtn').disabled = false;
  $('tagSearch').onkeydown = (e)=>{
    if(e.key === 'Enter'){ e.preventDefault(); tagsSearch(); }
  };
  selArt = retag ? {type:'album', url:''} : {type:'video', url:''};
  $('coverFile').value = '';
  $('coverPreview').classList.add('hidden');
  $('coverPreview').removeAttribute('src');
  $('uploadStatus').textContent = '';
  $('uploadStatus').className = 'upload-status';
  syncManualArt();
  setTagsBusy(false);
  $('tagCancelBtn').classList.remove('hidden');
  $('tagStartBtn').textContent = retag ? 'Apply tags to file' : 'Download with this match';
  $('manualSubmitBtn').textContent = retag ? 'Apply my tags' : 'Download with my tags';
  $('tagSkipBtn').textContent = retag ? 'Keep current tags' : 'Skip tags &amp; download';
  tagPane('match');
  setScreen('screenTags');
  openPicker(cands);
}
function closeTagPicker(){
  retagTaskId = null;
  setTagsBusy(false);
  $('tagCancelBtn').classList.add('hidden');
  tagPane('match');
  $('tagCard').classList.add('hidden');
}
async function editTagsForResult(){
  const taskId = pendingTaskId || (pendingTaskIds[0]);
  if(!taskId) return;
  try{
    const res = await apiFetch('/api/task/'+taskId+'/candidates?session='+encodeURIComponent(getSession()), {method:'GET', headers:{'X-Requested-With':'XMLHttpRequest'}});
    const data = await res.json();
    if(!res.ok || data.error) throw new Error(data.error || ('Request failed ('+res.status+')'));
    enterTags(taskId, data.candidates || [], true, data.query);
  }catch(e){
    showFeedback('Could not load tag candidates: '+((e&&e.message)||e), 'error');
  }
}
async function tagsStart(){
  const taskId = pendingTaskId;
  if(!taskId) return;
  if(!tagCands.length){ showFeedback('No song matches available.', 'error'); return; }
  setTagsBusy(true, true);
  const ok = await startWithSelection(taskId, tagCands);
  if(!ok) setTagsBusy(false);
}
async function tagsSearch(){
  const taskId = pendingTaskId;
  const input = $('tagSearch');
  if(!taskId) return;
  setTagsBusy(true);
  const q = (input.value || '').trim();
  try{
    const path = '/api/task/'+taskId+'/candidates?session='+encodeURIComponent(getSession())
      + (q ? '&q='+encodeURIComponent(q) : '');
    const res = await apiFetch(path, {method:'GET', headers:{'X-Requested-With':'XMLHttpRequest'}});
    const data = await res.json();
    if(!res.ok || data.error) throw new Error(data.error || ('Request failed ('+res.status+')'));
    input.value = data.query || q || '';
    openPicker(data.candidates || []);
    showFeedback('Search results for "' + (data.query || '') + '".', 'ok');
  }catch(e){
    showFeedback('Search failed: '+((e&&e.message)||e), 'error');
  }finally{
    setTagsBusy(false);
  }
}
async function tagsSkip(){
  const taskId = pendingTaskId;
  if(!taskId) return;
  if(retagTaskId){
    closeTagPicker();
    refreshResultAfterTags();
    return;
  }
  setTagsBusy(true, true);
  const ok = await postAndListen(taskId, {mode:'keep'});
  if(!ok) setTagsBusy(false);
}
async function tagsCancel(){
  const taskId = pendingTaskId;
  pendingTaskId = null;
  pendingTaskIds = pendingTaskIds.filter(t=>t!==taskId);
  const wasRetag = !!retagTaskId;
  retagTaskId = null;
  if(!wasRetag && taskId){
    // Only a deferred (pre-download) task is deleted on cancel; a retag picker
    // does not own the finished file.
    try{
      await apiFetch('/api/task/'+taskId+'?session='+encodeURIComponent(getSession()), {method:'DELETE', headers:{'X-Requested-With':'XMLHttpRequest'}});
    }catch(_){}
  }
  setTagsBusy(false);
  $('tagCancelBtn').classList.add('hidden');
  tagPane('match');
  setPrepareBtn(false, 'Prepare download');
  if(wasRetag){
    if(currentPlaylist){
      setScreen('screenConfigure');
      renderPlaylistResult();
    }else{
      setScreen('screenResult');
    }
  }else{
    setScreen('screenConfigure');
  }
}

function tagPane(which){
  const isManual = which === 'manual';
  $('tabMatch').classList.toggle('active', !isManual);
  $('tabManual').classList.toggle('active', isManual);
  $('paneMatch').classList.toggle('hidden', isManual);
  $('paneManual').classList.toggle('hidden', !isManual);
  if(isManual){
    const c = (tagCands[selCand] || {}).tags || {};
    $('mArtist').value = c.artist || '';
    $('mTitle').value = c.track || '';
    $('mAlbum').value = c.album || '';
    $('mTnum').value = c.track_number || '';
    $('mTtotal').value = c.track_total || '';
    $('mDisc').value = c.disc_number || '';
    $('mDtotal').value = c.disc_total || '';
    $('mGenre').value = c.genre || '';
    $('mYear').value = c.year || '';
    $('mComment').value = c.comment || '';
    if(selArt.type === 'album' || selArt.type === 'none'){
      selArt = {type:'video', url:''};
    }
    syncManualArt();
    $('mArtist').focus();
  }
}

function syncManualArt(){
  const t = selArt.type === 'file' ? 'file' : 'video';
  document.querySelectorAll('#manualArtSeg button').forEach(b=>{
    b.classList.toggle('active', b.dataset.art === t);
  });
  $('uploadPicker').classList.toggle('hidden', t !== 'file');
}

function setManualArt(t){
  $('uploadStatus').textContent = '';
  $('uploadStatus').className = 'upload-status';
  if(t === 'file'){
    selArt = {type:'file', url:''};
    $('coverFile').click();
  }else{
    selArt = {type:'video', url:''};
    $('coverPreview').classList.add('hidden');
    $('coverPreview').removeAttribute('src');
    $('coverFile').value = '';
  }
  syncManualArt();
}

async function uploadCover(){
  const f = $('coverFile').files[0];
  const st = $('uploadStatus');
  const prev = $('coverPreview');
  const taskId = pendingTaskId;
  if(!f || !taskId){
    selArt = {type:'video', url:''};
    syncManualArt();
    return;
  }
  if(f.size > 8*1024*1024){
    selArt = {type:'video', url:''};
    st.textContent = 'Image too large (max 8 MB).';
    st.className = 'upload-status error';
    syncManualArt();
    return;
  }
  st.innerHTML = '<span class="spinner"></span>Uploading…';
  st.className = 'upload-status';
  const fd = new FormData();
  fd.append('file', f);
  try{
    const res = await apiFetch('/api/art-local?task='+encodeURIComponent(taskId)
      +'&session='+encodeURIComponent(getSession())+authQuery(true),
      {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body:fd});
    const d = await res.json();
    if(!res.ok) throw new Error(d.error || ('Upload failed ('+res.status+')'));
    selArt = {type:'file', url: d.url};
    prev.src = URL.createObjectURL(f);
    prev.classList.remove('hidden');
    st.textContent = 'Will be embedded as the cover.';
    st.className = 'upload-status ok';
  }catch(e){
    st.textContent = e.message;
    st.className = 'upload-status error';
    selArt = {type:'video', url:''};
  }
  syncManualArt();
}

async function tagsManualSubmit(){
  const taskId = pendingTaskId;
  if(!taskId) return;
  const tags = {};
  const artist = $('mArtist').value.trim();
  const track  = $('mTitle').value.trim();
  const album  = $('mAlbum').value.trim();
  const tnum   = $('mTnum').value.trim();
  const ttotal = $('mTtotal').value.trim();
  const disc   = $('mDisc').value.trim();
  const dtotal = $('mDtotal').value.trim();
  const genre  = $('mGenre').value.trim();
  const year   = $('mYear').value.trim();
  const comment = $('mComment').value.trim();
  const hasAny = artist || track || album || tnum || ttotal || disc || dtotal || genre || year || comment;
  if(!hasAny){
    showFeedback('Enter at least one field (artist, title, album, track #, disc #, genre, year or comment).', 'error');
    return;
  }
  const numRe = /^\d{1,3}$/;
  if(tnum && !numRe.test(tnum)){ showFeedback('Track # must be a number (e.g. 5).', 'error'); return; }
  if(ttotal && !numRe.test(ttotal)){ showFeedback('Track total must be a number (e.g. 12).', 'error'); return; }
  if(disc && !numRe.test(disc)){ showFeedback('Disc # must be a number (e.g. 1).', 'error'); return; }
  if(dtotal && !numRe.test(dtotal)){ showFeedback('Disc total must be a number (e.g. 2).', 'error'); return; }
  if(artist) tags.artist = artist;
  if(track) tags.track = track;
  if(album) tags.album = album;
  if(tnum) tags.track_number = tnum;
  if(ttotal) tags.track_total = ttotal;
  if(disc) tags.disc_number = disc;
  if(dtotal) tags.disc_total = dtotal;
  if(genre) tags.genre = genre;
  if(year){
    if(!/^\d{4}$/.test(year)){ showFeedback('Year must be 4 digits (e.g. 2013).', 'error'); return; }
    tags.year = year;
  }
  if(comment) tags.comment = comment;
  const art = selArt;
  if(art.type === 'album'){
    const c = tagCands[selCand] || {};
    if(!c.artwork){ showFeedback('No album art available for the selected match — pick Album art on a match with a cover, or choose Video thumb.', 'error'); return; }
  }else if(art.type === 'file' && !art.url){
    showFeedback('Upload your cover image first, or choose Video thumb.', 'error');
    return;
  }
  setTagsBusy(true, true);
  const ok = retagTaskId
    ? await applyRetag(taskId, {mode:'manual', tags: tags, art: art})
    : await postAndListen(taskId, {mode:'manual', tags: tags, art: art});
  if(!ok) setTagsBusy(false);
}

function listenProgress(taskId){
  if(progressListeners[taskId]) return;
  progressListeners[taskId] = true;
  const fill = $('progressFill');
  const text = $('progressText');
  const POLL_MS = 2500;
  const STALL_MS = 8 * 60 * 1000;
  let failTries = 0;
  let lastChange = Date.now();
  let lastSig = '';
  let timer = null;

  function settle(){
    if(timer){ clearTimeout(timer); timer = null; }
    if(progressListeners[taskId]) delete progressListeners[taskId];
  }

  function showError(msg){
    settle();
    fill.className = 'progress-fill';
    text.textContent = msg;
    text.className = 'progress-text error';
    $('backBtn').classList.remove('hidden');
    setPrepareBtn(false, 'Prepare download');
    $('tagCard').classList.add('hidden');
  }

  function applyProgress(d){
    const phase = d.phase || 'downloading';
    if(d.indeterminate || d.percent === undefined || d.percent === null){
      fill.className = 'progress-fill indeterminate';
      if(phase==='extracting') text.textContent = 'Fetching video data…';
      else if(phase==='processing') text.textContent = 'Merging / converting…';
      else text.textContent = 'Preparing…';
      return;
    }
    const pct = Math.min(100, Math.max(0, d.percent||0));
    fill.className = 'progress-fill';
    fill.style.width = pct+'%';
    let txt = pct.toFixed(1)+'%';
    if(d.speed) txt += ' · '+d.speed;
    if(d.eta) txt += ' · ETA '+d.eta;
    if(phase==='processing') txt = 'Merging / converting…';
    text.textContent = txt;
  }

  const sig = (ev, d)=>{
    if(ev === 'progress') return ev + ':' + Math.round(Number(d.percent) || 0) + ':' + (d.phase||'');
    return ev || d.status || '';
  };

  const tick = async ()=>{
    let res, data;
    try{
      res = await apiFetch('/api/task/'+taskId+'?session='+encodeURIComponent(getSession()), {method:'GET'});
      data = await res.json();
    }catch(_){
      failTries++;
      if(failTries > 12){
        showError('Connection lost — the download may still finish in background');
        return;
      }
      text.textContent = 'Connection lost — retrying…';
      timer = setTimeout(tick, POLL_MS);
      return;
    }
    failTries = 0;
    const d = data.data || {};
    const ev = data.event
      || (data.status === 'ready' ? 'ready' : (data.status === 'failed' ? 'failed' : ''));
    const s = sig(ev, d);
    if(s !== lastSig){
      lastSig = s;
      lastChange = Date.now();
    }else if(Date.now() - lastChange > STALL_MS){
      showError('No progress for a while — may still be processing in the background');
      return;
    }
    if(ev === 'ready' || data.status === 'ready'){
      settle();
      fill.className = 'progress-fill';
      fill.style.width = '100%';
      text.textContent = 'Done!';
      text.className = 'progress-text success';
      setPrepareBtn(false, 'Prepare download');
      $('tagCard').classList.add('hidden');
      showReady(d);
      return;
    }
    if(ev === 'failed'){
      showError(d.message || 'Preparation failed');
      return;
    }
    if(ev === 'progress') applyProgress(d);
    text.className = 'progress-text';
    timer = setTimeout(tick, POLL_MS);
  };
  tick();
}

/* ---------- Song-tag picker (choose first, then start) ---------- */
function openPicker(cands){
  if(!cands.length) return;
  const same = tagCands === cands;
  if(!(same && selCand >= 0 && selCand < cands.length)){
    selCand = 0;
  }
  tagCands = cands;
  const win = cands[selCand] || {};
  if(selArt.type === 'album'){
    selArt = win.artwork ? {type:'album', url:win.artwork} : {type:'video', url:''};
  }else if(selArt.type !== 'none'){
    selArt = {type:'video', url:''};
  }
  paintCands(cands);
  syncArtSeg();
  $('tagCard').classList.remove('hidden');
}
function syncArtSeg(){
  document.querySelectorAll('#artSeg button').forEach(b=>{
    b.classList.toggle('active', b.dataset.art === selArt.type);
  });
}
function setArtType(t){
  const c = tagCands[selCand] || {};
  if(t === 'album'){
    selArt = c.artwork ? {type:'album', url:c.artwork} : {type:'video', url:''};
  }else if(t === 'none'){
    selArt = {type:'none', url:''};
  }else{
    selArt = {type:'video', url:''};
  }
  syncArtSeg();
}
function paintCands(cands){
  const list = $('tagCands');
  list.innerHTML = '';
  for(let i=0;i<cands.length;i++){
    const c = cands[i] || {};
    const t = c.tags || {};
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tag-cand' + (i===selCand ? ' active' : '');
    b.title = 'Select this match';
    const art = document.createElement('div');
    art.className = 'tc-artwrap';
    art.appendChild(artThumb(c.artwork));
    const score = document.createElement('span');
    score.className = 'tc-score';
    score.textContent = Math.round((c.score||0)*100)+'%';
    score.title = (c.source||'Catalog')+' match \u00b7 higher is a closer title match';
    art.appendChild(score);
    const check = document.createElement('span');
    check.className = 'tc-check';
    check.textContent = '\u2713';
    art.appendChild(check);
    b.appendChild(art);
    b.insertAdjacentHTML('beforeend',
      '<div class="tc-body">'
      + '<div class="tc-artist">'+esc(t.artist||'Unknown artist')+'</div>'
      + '<div class="tc-track">'+esc(t.track||'Untitled')+'</div>'
      + '<div class="tc-sub"><span class="tc-album">'+esc(t.album||'Single')+'</span>'
      + (t.year ? '<span class="tc-year">'+esc(t.year)+'</span>' : '')
      + '</div>'
      + '</div>');
    b.addEventListener('click', ()=>{
      selCand = i;
      if(selArt.type === 'album'){
        selArt = c.artwork ? {type:'album', url:c.artwork} : {type:'video', url:''};
      }else if(selArt.type === 'none'){
        selArt = {type:'none', url:''};
      }else{
        selArt = {type:'video', url:''};
      }
      paintCands(cands);
      syncArtSeg();
    });
    list.appendChild(b);
  }
}
function artThumb(url){
  const div = document.createElement('div');
  div.className = 'tc-art';
  if(url){
    const img = document.createElement('img');
    img.alt = '';
    img.src = '/api/art?u=' + encodeURIComponent(url) + authQuery(true);
    img.addEventListener('error', ()=>{ div.textContent = '\u266a'; });
    div.appendChild(img);
  }else{
    div.textContent = '\u266a';
  }
  return div;
}

/* ---------- Ready / preview ---------- */
function showReady(d){
  hasPreparedFile = true;
  lastReadyData = d;
  setScreen('screenResult');
  $('dlContext').classList.add('hidden');
  $('progressWrap').classList.add('hidden');
  $('backBtn').classList.remove('hidden');
  $('resultArea').classList.remove('hidden');
  const fileName = d.display_name || d.filename || 'file';
  const ext = (d.ext || '').toLowerCase();
  const audioExts = ['mp3','m4a','ogg','opus','wav'];
  const isAudio = audioExts.indexOf(ext) !== -1;
  $('resultName').textContent = fileName;
  $('resultSize').textContent = (d.size ? formatSize(d.size) : (d.ext || '').toUpperCase());
  $('editTagsBtn').classList.toggle('hidden', !isAudio);
  const dl = $('downloadBtn');
  dl.href = d.file_url + authQuery(false);
  dl.setAttribute('download', fileName);

  const coverMode = d.cover || 'video';
  if(d.artwork && coverMode === 'art'){
    $('resultArt').className = 'np-art';
    $('resultArt').src = d.artwork.indexOf('/') === 0
      ? d.artwork + authQuery(false)
      : '/api/art?u=' + encodeURIComponent(d.artwork) + authQuery(true);
  }else if(coverMode === 'video'){
    const th = (currentInfo && currentInfo.thumbnail) || '';
    if(th){
      $('resultArt').className = 'np-art';
      $('resultArt').src = th;
    }else{
      $('resultArt').className = 'np-art hidden';
      $('resultArt').removeAttribute('src');
    }
  }else{
    $('resultArt').className = 'np-art hidden';
    $('resultArt').removeAttribute('src');
  }

  const meta = $('resultMeta');
  meta.classList.add('hidden');
  meta.innerHTML = '';
  const unknownTag = /^(?:unknown (?:track|artist)|na|n\/a|n\\a)$/i;
  const metaRow = function(cls, txt){
    const el = document.createElement('div');
    el.className = cls;
    el.textContent = txt;
    meta.appendChild(el);
  };
  if(isAudio || (d.tags && (d.tags_mode === 'candidate' || d.tags_mode === 'manual'))){
    if(d.tags && (d.tags_mode === 'candidate' || d.tags_mode === 'manual')){
      const t = d.tags;
      if(t.track && !unknownTag.test(String(t.track))) $('resultName').textContent = String(t.track);
      if(t.artist && !unknownTag.test(String(t.artist))) metaRow('meta-artist', String(t.artist));
      if(t.album) metaRow('meta-album', String(t.album));
      if(t.year) metaRow('meta-year', String(t.year));
    }else{
      const info = currentInfo || {};
      if(info.creator || info.uploader) metaRow('meta-artist', info.creator || info.uploader);
    }
    if(meta.children.length) meta.classList.remove('hidden');
  }

  const v = $('previewVideo');
  const a = $('previewAudio');
  const ctrl = $('npControls');
  v.pause(); v.removeAttribute('src'); v.load();
  a.pause(); a.removeAttribute('src'); a.load();
  resetPlayerUI();
  const videoExts = ['mp4','webm'];
  const fb = $('previewFallback');
  const canPreview = d.preview_url && (videoExts.indexOf(ext) !== -1 || audioExts.indexOf(ext) !== -1);
  $('npNowPlaying').classList.toggle('hidden', !canPreview);

  if(d.preview_url && videoExts.indexOf(ext) !== -1){
    v.classList.remove('hidden');
    a.classList.add('hidden');
    ctrl.classList.add('hidden');
    fb.style.display = 'none';
    v.src = d.preview_url + authQuery(true);
  }else if(d.preview_url && audioExts.indexOf(ext) !== -1){
    v.classList.add('hidden');
    a.classList.add('hidden');
    ctrl.classList.remove('hidden');
    fb.style.display = 'none';
    a.src = d.preview_url + authQuery(true);
    resetPlayerUI();
  }else{
    v.classList.add('hidden');
    a.classList.add('hidden');
    ctrl.classList.add('hidden');
    fb.style.display = 'flex';
  }
}

/* ---------- Audio player bar ---------- */
const PLAY_ICON = '<svg class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const PAUSE_ICON = '<svg class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/></svg>';
function fmtTime(s){
  s = Math.floor(s || 0);
  if(!isFinite(s)) s = 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? h + ':' + mm : mm) + ':' + sec;
}
function setPlayIcon(playing){
  $('npPlayIcon').innerHTML = playing ? PAUSE_ICON : PLAY_ICON;
}
function updateNp(){
  const a = $('previewAudio');
  const dur = isFinite(a.duration) ? a.duration : 0;
  const cur = a.currentTime || 0;
  $('npTime').textContent = fmtTime(cur) + ' / ' + fmtTime(dur);
  $('npSeekFill').style.width = dur ? Math.min(100, (cur / dur) * 100) + '%' : '0%';
}
function resetPlayerUI(){
  $('npTime').textContent = '0:00 / 0:00';
  $('npSeekFill').style.width = '0%';
  setPlayIcon(false);
}
(function initAudioPlayer(){
  const a = $('previewAudio');
  a.addEventListener('loadedmetadata', updateNp);
  a.addEventListener('timeupdate', updateNp);
  a.addEventListener('play', ()=> setPlayIcon(true));
  a.addEventListener('pause', ()=> setPlayIcon(false));
  a.addEventListener('ended', ()=>{ setPlayIcon(false); $('npSeekFill').style.width = '100%'; });
  $('npPlayBtn').addEventListener('click', ()=>{
    a.paused ? a.play().catch(()=>{}) : a.pause();
  });
  const seek = $('npSeek');
  seek.addEventListener('click', (e)=>{
    const r = seek.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - r.left) / (r.width || 1)));
    if(isFinite(a.duration) && a.duration) a.currentTime = pct * a.duration;
  });
  seek.addEventListener('keydown', (e)=>{
    if(e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    if(isFinite(a.duration) && a.duration){
      const d = e.key === 'ArrowRight' ? 5 : -5;
      a.currentTime = Math.max(0, Math.min(a.duration, a.currentTime + d));
    }
  });
})();

/* ---------- Playlist ---------- */
function showPlaylist(url, data){
  currentPlaylist = {url, entries: data.entries||[]};
  $('urlPill').textContent = url;
  $('urlPill').title = url;
  setScreen('screenConfigure');
  $('tagCard').classList.add('hidden');
  $('infoCard').classList.add('hidden');
  $('prepareBtn').classList.add('hidden');
  $('playlistCard').classList.remove('hidden');
  populateFormats();
  $('playlistTitle').textContent = data.title || 'Playlist';
  const pMode = playlistModeInfo();
  $('playlistCount').textContent = currentPlaylist.entries.length + ' items'
    + (data.entries_truncated ? ' (showing first 500)' : '');
  $('prepareAllBtn').innerHTML = 'Prepare All (' + pMode.badge + ')';
  const list = $('playlistList');
  list.innerHTML = '';
  for(const entry of currentPlaylist.entries){
    const div = document.createElement('div');
    div.className = 'item';
    div.innerHTML = '<div class="ptitle">'+esc(entry.title)+'</div>'
      + '<div class="pmeta"><span class="idx">#'+entry.index+'</span><span class="badge">'+pMode.badge+'</span></div>';
    list.appendChild(div);
  }
}

let prepareQueue = [];
let prepareRunning = false;
async function prepareAll(){
  if(!currentPlaylist || !currentPlaylist.entries.length) return;
  $('prepareAllBtn').disabled = true;
  $('prepareAllBtn').innerHTML = '<span class="spinner"></span> Preparing';
  const pwrap = $('plProgressWrap'), pt = $('plProgressText'), pf = $('plProgressFill');
  pwrap.classList.remove('hidden');
  pt.textContent = 'Preparing playlist…';
  pf.className = 'progress-fill indeterminate';
  $('resultArea').classList.add('hidden');
  $('plResult').classList.add('hidden');
  $('plResultList').innerHTML = '';
  stopPlPreview();
  pendingTaskIds = [];
  hasPreparedFile = false;
  preparedResults = [];
  $('tagCard').classList.add('hidden');
  $('tagCands').innerHTML = '';
  const pMode = playlistModeInfo();
  prepareQueue = currentPlaylist.entries.map(e=>({...e, mode:pMode.mode, format:pMode.format}));
  prepareFailures = [];
  prepareCancelled = false;
  prepareRunning = true;
  setBusy(true);
  await processQueue();
}

async function processQueue(){
  const pt = $('plProgressText'), pf = $('plProgressFill');
  if(!prepareRunning || !prepareQueue.length){
    $('prepareAllBtn').disabled = false;
    $('prepareAllBtn').innerHTML = 'Prepare All (' + playlistModeInfo().badge + ')';
    setBusy(false);
    pf.className = 'progress-fill';
    pf.style.width = '100%';
    renderPlaylistResult();
    if(prepareCancelled){
      pt.className = 'progress-text';
      pt.textContent = 'Cancelled — no files were deleted.';
      return;
    }
    if(prepareFailures.length){
      pt.className = 'progress-text error';
      pt.innerHTML = prepareFailures.length + ' of ' + currentPlaylist.entries.length + ' could not be prepared:';
      for(const f of prepareFailures.slice(0, 3)){
        pt.insertAdjacentHTML('beforeend', '<div class="pl-fail">\u00b7 ' + esc(f.title) + ' \u2014 ' + esc(f.error) + '</div>');
      }
      if(prepareFailures.length > 3) pt.insertAdjacentHTML('beforeend', '<div>…</div>');
      return;
    }
    pt.textContent = 'All ' + preparedResults.length + ' of ' + currentPlaylist.entries.length + ' downloaded!';
    pt.className = 'progress-text success';
    if(pendingTaskIds.length) hasPreparedFile = true;
    return;
  }
  const entry = prepareQueue[0];
  const total = currentPlaylist.entries.length;
  const completed = total - prepareQueue.length;
  const basePct = completed / total * 100;
  const perItemPct = 100 / total;
  pt.className = 'progress-text';
  pt.textContent = 'Preparing #'+entry.index+' of '+total+': '+entry.title;
  pf.className = 'progress-fill';
  pf.style.width = basePct+'%';
  try{
    const res = await fetchWithTimeout('/api/prepare',{
      method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({url:entry.url, mode:entry.mode, format:entry.format, batch:true, session: getSession(), title: entry.title, tags: entry.mode === 'audio'}),
    }, 120000);
    const data = await res.json();
    if(!res.ok || data.error){
      prepareFailures.push({title: entry.title, error: data.error || ('Request failed ('+res.status+')')});
    }else{
      pendingTaskIds.push(data.task_id);
      const out = await waitForTask(data.task_id,
        (d)=>{
          pf.style.width = (basePct + d.percent/100 * perItemPct)+'%';
          pt.textContent = 'Downloading #'+entry.index+' of '+total+' — '+d.percent.toFixed(0)+'%'
            + (d.eta ? ' · ETA '+d.eta : '');
        },
        (s)=>{
          if(s){
            pt.className = 'progress-text';
            pt.textContent = s;
          }
        });
      if(out && out.ok){
        preparedResults.push({index: entry.index, title: entry.title, taskId: data.task_id, ready: out.data || {}});
      }else if(out){
        prepareFailures.push({title: entry.title, error: out.error || 'preparation failed'});
      }
    }
  }catch(e){
    prepareFailures.push({title: entry.title, error: (e&&e.message)||'network error'});
  }
  prepareQueue.shift();
  if(!prepareRunning) return;
  await processQueue();
}

function waitForTask(taskId, onProgress, onStatus){
  return new Promise((resolve)=>{
    const POLL_MS = 2500;
    const STALL_MS = 8 * 60 * 1000;
    let failTries = 0;
    let lastChange = Date.now();
    let lastSig = '';
    let timer = null;
    const stop = (res)=>{
      if(timer){ clearTimeout(timer); timer = null; }
      if(onStatus) onStatus('');
      resolve(res);
    };
    const sig = (ev, d)=>{
      if(ev === 'progress') return ev + ':' + Math.round(Number(d.percent) || 0);
      return ev || d.status || '';
    };
    const tick = async ()=>{
      let res, data;
      try{
        res = await apiFetch('/api/task/'+taskId+'?session='+encodeURIComponent(getSession()), {method:'GET'});
        data = await res.json();
      }catch(_){
        failTries++;
        if(failTries > 12){
          stop({ok:false, error:'connection lost — download may still finish in background'});
          return;
        }
        if(onStatus) onStatus('Connection lost — retrying…');
        timer = setTimeout(tick, POLL_MS);
        return;
      }
      failTries = 0;
      const d = data.data || {};
      const ev = data.event
        || (data.status === 'ready' ? 'ready' : (data.status === 'failed' ? 'failed' : ''));
      const s = sig(ev, d);
      if(s !== lastSig){
        lastSig = s;
        lastChange = Date.now();
      }else if(Date.now() - lastChange > STALL_MS){
        stop({ok:false, error:'no progress for a while — continuing to the next track'});
        return;
      }
      if(onStatus) onStatus('');
      if(ev === 'ready' || data.status === 'ready'){ stop({ok:true, data:d}); return; }
      if(ev === 'failed'){ stop({ok:false, error:d.message || 'Preparation failed'}); return; }
      if(ev === 'progress' && typeof d.percent === 'number' && onProgress){
        onProgress(d);
      }
      timer = setTimeout(tick, POLL_MS);
    };
    tick();
  });
}

const plPrev = {a: null, btn: null};

function setPlBtnIcon(btn, playing){
  if(!btn) return;
  btn.classList.toggle('playing', !!playing);
  btn.setAttribute('aria-pressed', playing ? 'true' : 'false');
  btn.innerHTML = playing ? PAUSE_ICON : PLAY_ICON;
}

function stopPlPreview(){
  if(plPrev.a){
    plPrev.a.pause();
    plPrev.a.removeAttribute('src');
    plPrev.a.load();
  }
  setPlBtnIcon(plPrev.btn, false);
  plPrev.btn = null;
}

function togglePlPreview(btn){
  const url = btn.getAttribute('data-preview-url');
  if(!url) return;
  const a = plPrev.a || (plPrev.a = new Audio());
  a.onended = ()=>{ setPlBtnIcon(plPrev.btn, false); plPrev.btn = null; };
  a.onerror = ()=>{ setPlBtnIcon(plPrev.btn, false); plPrev.btn = null; };
  if(plPrev.btn === btn){
    if(!a.paused){
      a.pause();
      setPlBtnIcon(btn, false);
      plPrev.btn = null;
    }
    return;
  }
  setPlBtnIcon(plPrev.btn, false);
  a.src = url;
  a.load();
  a.play().then(()=>{
    plPrev.btn = btn;
    setPlBtnIcon(btn, true);
  }).catch(()=>{
    setPlBtnIcon(btn, false);
    plPrev.btn = null;
  });
}

const noteSvg = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';

function renderPlaylistResult(){
  const box = $('plResult'), list = $('plResultList');
  stopPlPreview();
  if(!preparedResults.length){ box.classList.add('hidden'); return; }
  $('plResultSummary').textContent = 'Downloaded ' + preparedResults.length + ' of ' + currentPlaylist.entries.length;
  list.classList.add('pl-result-list');
  list.innerHTML = '';
  const unknownTag = /^(?:unknown (?:track|artist)|na|n\/a|n\\a)$/i;
  const audioExts = ['mp3','m4a','ogg','opus','wav'];
  for(const r of preparedResults){
    const d = r.ready || {};
    const row = document.createElement('div');
    row.className = 'pl-row';
    const url = d.file_url ? (d.file_url + authQuery(false)) : '#';
    let name = d.title || r.title || d.display_name || d.filename || 'file';
    let tagsLine = '';
    if(d.tags && (d.tags_mode === 'candidate' || d.tags_mode === 'manual' || d.tags_mode === 'auto')){
      const t = d.tags || {};
      if(t.track && !unknownTag.test(String(t.track))) name = String(t.track);
      const parts = [];
      if(t.artist && !unknownTag.test(String(t.artist))) parts.push(String(t.artist));
      if(t.album && !unknownTag.test(String(t.album))) parts.push(String(t.album));
      if(t.year) parts.push(String(t.year));
      if(parts.length) tagsLine = parts.join(' \u00b7 ');
    }else if(d.creator && !unknownTag.test(String(d.creator))){
      tagsLine = String(d.creator);
    }
    const size = d.size ? formatSize(d.size) : null;
    const ext = (d.ext || '').toUpperCase();
    const badge = [size, ext].filter(Boolean).join(' \u00b7 ');
    const isAudio = audioExts.indexOf(String(d.ext || '').toLowerCase()) !== -1;
    let art;
    if(d.artwork && d.cover === 'art'){
      const artSrc = d.artwork.indexOf('/') === 0
        ? d.artwork + authQuery(false)
        : '/api/art?u=' + encodeURIComponent(d.artwork) + authQuery(true);
      art = '<img class="pl-art" src="'+esc(artSrc)+'" alt="" loading="lazy">';
    }else if(d.thumbnail){
      art = '<img class="pl-art" src="'+esc(d.thumbnail)+'" alt="" loading="lazy">';
    }else{
      art = '<div class="pl-art pl-art-empty">'+noteSvg+'</div>';
    }
    const playBtn = isAudio
      ? '<button type="button" class="pl-play" data-preview-url="'+(d.preview_url ? esc(d.preview_url + authQuery(true)) : '')+'" aria-label="Preview" aria-pressed="false">'+PLAY_ICON+'</button>'
      : '';
    const editBtn = isAudio
      ? '<button type="button" class="btn btn-ghost btn-sm edit-tags-btn" data-task-id="'+esc(r.taskId||'')+'" data-index="'+esc(String(r.index||''))+'">Edit tags</button>'
      : '';
    row.innerHTML =
      art +
      '<div class="pl-body">' +
        '<div class="pl-name">'+esc(name)+'</div>' +
        (tagsLine ? '<div class="pl-tags">'+esc(tagsLine)+'</div>' : '') +
        '<div class="pl-meta">' +
          '<span class="pl-idx">#'+esc(String(r.index||''))+'</span>' +
          '<span class="badge-size">'+esc(badge)+'</span>' +
        '</div>' +
      '</div>' +
      '<div class="pl-actions">' +
        playBtn +
        '<a class="btn btn-ghost btn-sm save-btn" href="'+esc(url)+'" download="'+esc(name)+'">Save</a>' +
        editBtn +
      '</div>';
    list.appendChild(row);
  }
  list.querySelectorAll('.pl-play').forEach(btn=>{
    btn.addEventListener('click', ()=>togglePlPreview(btn));
  });
  list.querySelectorAll('.edit-tags-btn').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      const taskId = btn.dataset.taskId;
      if(!taskId) return;
      try{
        const res = await apiFetch('/api/task/'+taskId+'/candidates?session='+encodeURIComponent(getSession()), {method:'GET', headers:{'X-Requested-With':'XMLHttpRequest'}});
        const data = await res.json();
        if(!res.ok || data.error) throw new Error(data.error || ('Request failed ('+res.status+')'));
        enterTags(taskId, data.candidates || [], true, data.query);
      }catch(e){
        showFeedback('Could not load tag candidates: '+((e&&e.message)||e), 'error');
      }
    });
  });
  box.classList.remove('hidden');
}

function saveAllPrepared(){
  const links = $('plResultList').querySelectorAll('a[download]');
  links.forEach((a, i)=>{
    setTimeout(()=>{ try{ a.click(); }catch(_){} }, i * 300);
  });
}

async function fetchWithTimeout(path, opts, ms){
  const ctl = new AbortController();
  const timer = setTimeout(()=>ctl.abort(), ms);
  try{
    return await apiFetch(path, Object.assign({}, opts, {signal: ctl.signal}));
  }finally{
    clearTimeout(timer);
  }
}

function esc(s){
  const d=document.createElement('div');d.textContent=s;return d.innerHTML;
}

/* ---------- Home (deletes files + resets everything) ---------- */
async function homeReset(ev){
  if(ev && ev.preventDefault) ev.preventDefault();
  if(pendingTaskIds.length || hasPreparedFile){
    const suffix = pendingTaskIds.length > 1 ? 's' : '';
    const verb = busyDownload ? 'cancel the running task' : 'delete the prepared file';
    if(!window.confirm('Going home will ' + verb + suffix + '. This cannot be undone.')){
      return;
    }
  }
  prepareCancelled = true;
  prepareRunning = false;
  prepareQueue = [];
  prepareFailures = [];
  setBusy(false);
  for(const id of pendingTaskIds){
    try{ await apiFetch('/api/task/'+id+'?session='+encodeURIComponent(getSession()), {method:'DELETE'}); }catch(_){}
  }
  pendingTaskIds = [];
  pendingTaskId = null;
  setTagsBusy(false);
  $('tagCancelBtn').classList.add('hidden');
  tagPane('match');
  hasPreparedFile = false;
  currentInfo = null;
  currentPlaylist = null;
  currentMode = 'video';
  $('url').value = '';
  $('url').disabled = false;
  $('resultArea').classList.add('hidden');
  $('plResult').classList.add('hidden');
  $('plResultList').innerHTML = '';
  stopPlPreview();
  $('infoCard').classList.add('hidden');
  $('prepareBtn').classList.add('hidden');
  $('playlistCard').classList.add('hidden');
  $('progressWrap').classList.add('hidden');
  $('dlContext').classList.add('hidden');
  $('tagCard').classList.add('hidden');
  $('tagCands').innerHTML = '';
  showFeedback('');
  $('modeVideo').classList.add('active');
  $('modeAudio').classList.remove('active');
  $('backBtn').classList.add('hidden');
  setPrepareBtn(false, 'Prepare download');
  setScreen('screenImport');
}

window.addEventListener('beforeunload',(e)=>{
  if(hasPreparedFile){
    e.preventDefault();
    e.returnValue = '';
  }
});

window.addEventListener('pagehide',()=>{
  if(hasPreparedFile){
    for(const id of pendingTaskIds){
      try{ navigator.sendBeacon('/api/task/'+id+'?session='+encodeURIComponent(getSession()) + authQuery(true)); }catch(_){}
    }
  }
});

$('url').addEventListener('keydown',(e)=>{
  if(e.key==='Enter') fetchInfo();
});
