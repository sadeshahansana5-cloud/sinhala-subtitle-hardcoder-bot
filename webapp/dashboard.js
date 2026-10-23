(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const initData = tg && tg.initData ? tg.initData : '';
  const $ = (id) => document.getElementById(id);
  const state = { data: null, busy: false, polling: false, styleDirty: false, watermarkDirty: false, pendingVideo: null, pendingSrt: null, previewUrl: '', previewCues: [], syncStage: 'idle', syncFile: null, poll: null };
  const statusEl = $('connection');
  const toastEl = $('toast');
  let toastTimer;
  if (tg) { try { tg.ready(); tg.expand(); tg.setHeaderColor('#141718'); tg.setBackgroundColor('#141718'); } catch (_) {} }

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const bytes = (n) => { n = Number(n || 0); if (!n) return '0 B'; const u=['B','KB','MB','GB','TB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; };
  const errorSi = (value) => {
    const text=String(value||'').toLowerCase();
    if(!text) return 'ඉල්ලීම සාර්ථක වුණේ නැහැ. නැවත උත්සාහ කරන්න.';
    if(/[\u0d80-\u0dff]/.test(String(value))) return String(value);
    if(text.includes('cancelled')||text.includes('canceled')) return 'අවලංගු කළා';
    if(text.includes('login')||text.includes('unauthorized')||text.includes('authentication')) return 'Telegram Web App එකෙන් නැවත විවෘත කර උත්සාහ කරන්න.';
    if(text.includes('track')||text.includes('subtitle')||text.includes('srt')) return 'තෝරාගත් track එක හෝ SRT ගොනුව පරීක්ෂා කර නැවත උත්සාහ කරන්න.';
    if(text.includes('video')||text.includes('source')) return 'වීඩියෝ ගොනුව තවම තිබේදැයි බලන්න; අවශ්‍ය නම් නැවත upload කරන්න.';
    if(text.includes('running')||text.includes('active job')||text.includes('current job')) return 'දැනට කාර්යයක් ක්‍රියාත්මකයි. එය අවසන් වනතෙක් රැඳී සිටින්න.';
    if(text.includes('large')||text.includes('limit')||text.includes('too big')) return 'ගොනුව අවසර දී ඇති ප්‍රමාණයට වඩා විශාලයි.';
    if(text.includes('network')||text.includes('fetch')) return 'සම්බන්ධතාව බිඳ වැටුණා. නැවත උත්සාහ කරන්න.';
    return 'ඉල්ලීම සාර්ථක වුණේ නැහැ. ගොනු තත්ත්වය පරීක්ෂා කර නැවත උත්සාහ කරන්න.';
  };
  const duration = (s) => { s = Math.max(0, Math.floor(Number(s || 0))); const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60; return h ? `${h}පැ ${m}මි` : `${m}මි ${sec}ත` };
  const srtSeconds = (v) => { const m=String(v).replace(',', '.').match(/(\d+):(\d{2}):(\d{2})\.(\d{3})/); return m?Number(m[1])*3600+Number(m[2])*60+Number(m[3])+Number(m[4])/1000:0; };
  function setLocalVideo(file){if(!file)return;const video=$('local-preview');if(state.previewUrl)URL.revokeObjectURL(state.previewUrl);state.previewUrl=URL.createObjectURL(file);video.src=state.previewUrl;video.hidden=false;$('preview-empty').hidden=true;$('preview-file-name').textContent=file.name;renderPreviewCue();}
  async function setLocalSrt(file){if(!file)return;try{const text=await file.text(),rx=/(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})[^\r\n]*\r?\n([\s\S]*?)(?=\r?\n\s*\r?\n|$)/g;state.previewCues=[...text.matchAll(rx)].map(m=>({start:srtSeconds(m[1]),end:srtSeconds(m[2]),text:m[3].replace(/<[^>]*>/g,'').trim()})).filter(c=>c.text);renderPreviewCue();}catch(_){state.previewCues=[];}}
  function renderPreviewCue(){const v=$('local-preview'),el=$('subtitle-overlay');if(!v||v.hidden||!v.src){el.hidden=true;return;}const playbackTime=v.currentTime-Number($('delay-input')?.value||0)/1000;const cue=state.previewCues.find(c=>playbackTime>=c.start&&playbackTime<c.end);if(!cue){el.hidden=true;return;}el.innerHTML=esc(cue.text).replace(/\n/g,'<br>');el.hidden=false;const align=Number($('alignment')?.value||2);el.style.textAlign=align%3===1?'left':align%3===0?'right':'center';el.style.top=align>=7?`${Math.max(2,Number($('margin-v')?.value||24)/8)}%`:'auto';el.style.bottom=align<7?`${Math.max(3,Number($('margin-v')?.value||24)/8)}%`:'auto';el.style.fontSize=`${Math.max(12,Math.min(54,Number($('font-size')?.value||52)*.42))}px`;el.style.fontFamily=$('font-select')?.value||'system-ui, sans-serif';el.style.fontWeight=$('bold')?.checked?'700':'500';el.style.color=$('primary-color')?.value||'#ffffff';el.style.webkitTextStroke=`${Math.max(0,Number($('outline-width')?.value||0)*.35)}px ${$('outline-color')?.value||'#000000'}`;el.style.textShadow=`0 2px ${Math.max(1,Number($('shadow-width')?.value||1)*1.5)}px #000,0 0 8px #000`;}
  const dateText = (t) => { if (!t) return '—'; const d=new Date(Number(t)*1000); return d.toLocaleString('si-LK',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); };
  const flash = (text, kind='') => { toastEl.textContent=kind==='error'?errorSi(text):text; toastEl.className=`toast show ${kind}`; clearTimeout(toastTimer); toastTimer=setTimeout(()=>toastEl.className='toast',2700); };
  const setFeedback = (id,text,kind='') => { const el=$(id); if(!el)return; el.textContent=kind==='error'?errorSi(text):text; el.className=`feedback ${kind}`; };
  const setConnection = (ok) => { statusEl.classList.toggle('off',!ok); statusEl.querySelector('span').textContent=ok?'සජීවීව සම්බන්ධයි':'සම්බන්ධතාව නැවත උත්සාහ කරයි'; };
  const api = async (path, options={}) => {
    const headers = new Headers(options.headers || {});
    headers.set('X-Telegram-Init-Data', initData);
    if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type','application/json');
    const response = await fetch(path,{...options,headers,cache:'no-store'});
    const text = await response.text(); let body={}; try { body=JSON.parse(text); } catch (_) { body={message:text}; }
    if(!response.ok) throw new Error(errorSi(body.message || body.reason || `Request failed (${response.status})`));
    return body;
  };
  const post = (path, payload) => api(path,{method:'POST',body:JSON.stringify(payload)});

  function showGate(){ $('auth-gate').hidden=false; $('app').hidden=true; setConnection(false); }
  function setupTabs(){ document.querySelectorAll('.tab').forEach(btn=>btn.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===btn));document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===`view-${btn.dataset.view}`));window.scrollTo({top:0,behavior:'smooth'});})); }

  async function loadState(){
    if(!initData||state.polling) return;
    state.polling=true;
    try { const data=await api('/dashboard/api/state'); state.data=data; setConnection(true); $('app').hidden=false; $('auth-gate').hidden=true; render(data); }
    catch(e){ setConnection(false); if(String(e.message).includes('login')) showGate(); else if(!state.data) flash(e.message,'error'); }
    finally { state.polling=false; }
  }
  function render(data){
    const first=(tg?.initDataUnsafe?.user?.first_name||'').trim();
    if(first) $('greeting').textContent=`ආයුබෝවන් ${first} — අද ඔබේ video වැඩ මෙතැනින් පාලනය කරන්න.`;
    $('last-updated').textContent=`යාවත්කාලීන කළේ ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}`;
    const st=data.stats||{};
    $('metrics').innerHTML=[
      ['සියලු jobs','▦',st.total||0,'සම්පූර්ණ ඉතිහාසය'],['සාර්ථක','✓',st.completed||0,'සාර්ථකව නිමවූ'],
      ['දෝෂ','!',st.failed||0,'අසාර්ථකව අවසන් වූ'],['නැවැත්වූ','×',st.cancelled||0,'අවලංගු කළ jobs'],
      ['අද','◷',st.today||0,'අද ආරම්භ කළ'],['ගබඩාව','▣',bytes(data.storage_bytes),'තාවකාලික files සහ fonts']
    ].map(x=>`<div class="metric"><div class="metric-head"><span>${x[0]}</span><span class="metric-icon">${x[1]}</span></div><b>${x[2]}</b><small>${x[3]}</small></div>`).join('');
    renderActive(data.active_job); renderPending(data.pending_delivery); renderFiles(data); renderTracks(data); renderHistory(data.history||[]); renderActivity(data.daily||[]); if(!state.styleDirty){renderFonts(data.fonts||[],data.style||{});applyStyle(data.style||{});} if(!state.watermarkDirty)$('watermark-text').value=data.watermark||''; $('storage-value').textContent=bytes(data.storage_bytes);
    if(data.active_job?.kind==='sync_wait')state.syncStage=String(data.active_job.stage||'').includes('සිංහල')?'si':'en';else if(data.active_job?.kind==='sync')state.syncStage='running';else if(state.syncStage==='running')state.syncStage='done';
    updateSyncUI(data.active_job);
    const op=$('operation').value;
    const hasExtractTrack=op==='audio_extract' ? !!data.video?.media?.audio?.length : op==='subtitle_extract' ? !!data.video?.media?.subs?.some(t=>t.extractable) : true;
    const needsSrt=op==='hardcode'||op==='embed';
    const canProcess=!!(data.video&&(!needsSrt||data.subtitle?.ready)&&hasExtractTrack&&!data.active_job);
    $('start-job').disabled=!canProcess;
    $('upload-selected').disabled=state.busy||(!state.pendingVideo&&!state.pendingSrt);
    $('operation').disabled=!!data.active_job;
    $('mode').disabled=!!data.active_job;
    $('full-video').disabled=!!data.active_job;
    $('watermark').disabled=!!data.active_job;
    $('delay-input').disabled=!!data.active_job;
    $('name-input').disabled=!!data.active_job;
    updateOperationUI();
    $('footer-state').textContent=data.active_job?`සජීවී කාර්යය: ${data.active_job.stage||'වැඩ කරමින්'}`:'Telegram සමඟ ආරක්ෂිතව සම්බන්ධයි';
  }
  function renderActive(job){
    const card=$('active-card');
    if(!job){card.hidden=true;return;}
    card.hidden=false; const pct=Math.max(0,Math.min(100,Number(job.progress||0))); const circ=2*Math.PI*34; const offset=circ*(1-pct/100);
    const label=job.kind==='hardcode'?'🔥 උපසිරැසි වීඩියෝවට දමන්න':job.kind==='embed'?'🧩 උපසිරැසි එක් කිරීම':job.kind==='xa'?'🎧 ශ්‍රව්‍ය ලබාගැනීම':job.kind==='xs'?'💬 උපසිරැසි ලබාගැනීම':job.kind==='sync'?'🔄 උපසිරැසි ගැළපීම පරීක්ෂා කිරීම':job.kind==='sync_wait'?'🔄 ගැළපීම සඳහා ගොනු බලා සිටීම':esc(job.kind||'කාර්යය');
    card.innerHTML=`<div class="job-main"><svg class="ring" viewBox="0 0 76 76" role="img" aria-label="${pct.toFixed(0)} percent"><defs><linearGradient id="progress-gradient"><stop stop-color="#43c8ff"/><stop offset="1" stop-color="#8a80ff"/></linearGradient></defs><circle class="track" cx="38" cy="38" r="34"/><circle class="value" cx="38" cy="38" r="34" stroke-dasharray="${circ}" stroke-dashoffset="${offset}"/><text x="38" y="41" text-anchor="middle">${pct.toFixed(0)}%</text></svg><div class="job-titles"><span class="eyebrow"><i class="live-dot"></i> සජීවී කාර්යය</span><h3>${label} • ${esc(job.stage||'සකස් කරමින්')}</h3><p>${esc(job.status||'සජීවීව ක්‍රියාත්මකයි')}</p></div><div class="job-actions"><button class="button button-danger-quiet" id="cancel-job">■ නවත්වන්න</button></div></div><div class="job-meta"><span>⏱ ගතවූ කාලය <b>${duration(job.elapsed)}</b></span>${job.eta?`<span>≈ ඉතිරි <b>${duration(job.eta)}</b></span>`:''}${job.total_bytes?`<span>⇧ ${bytes(job.current_bytes)} / ${bytes(job.total_bytes)}</span>`:''}</div><div class="bar"><i style="width:${pct}%"></i></div>${job.can_set_name?`<form id="active-name-form" class="pending-form"><input id="active-output-name" maxlength="120" placeholder="වීඩියෝව සකස් වන අතරතුර නම (අග කොටස නොමැතිව)"><button class="button button-secondary">නම සුරකින්න</button><button type="button" class="button button-quiet" id="active-name-skip">නම නොදමන්න</button></form>`:''}`;
    $('cancel-job').addEventListener('click',async()=>{if(!window.confirm('ක්‍රියාත්මක කාර්යය නවත්වන්නද?'))return;try{await post('/dashboard/api/cancel',{});flash('නවත්වන ඉල්ලීම යැවුවා');await loadState();}catch(e){flash(e.message,'error');}});
    const form=$('active-name-form'); if(form){form.addEventListener('submit',async e=>{e.preventDefault();try{const r=await post('/dashboard/api/name',{action:'set',name:$('active-output-name').value});flash(`නම තෝරාගත්තා: ${r.name}`);await loadState();}catch(err){flash(err.message,'error');}});$('active-name-skip').addEventListener('click',async()=>{try{await post('/dashboard/api/name',{action:'skip'});flash('මුල් නම තෝරාගත්තා');await loadState();}catch(e){flash(e.message,'error');}});}
  }
  function renderPending(p){
    const card=$('pending-card'); if(!p){card.hidden=true;return;} card.hidden=false;
    const left=Math.max(0,Math.ceil((Number(p.auto_send_at||0)-Date.now()/1000)/60));
    card.innerHTML=`<div class="pending-head"><div class="panel-icon">◷</div><div><h3>ප්‍රතිදාන ගොනුව යැවීමට සූදානම්</h3><p><code>${esc(p.name)}</code> • ${bytes(p.size)} • නමක් නොදුන්නොත් මිනිත්තු ${left}කින් ස්වයංක්‍රීයව යැවෙනු ඇත</p></div></div><form id="pending-name-form" class="pending-form"><input id="pending-output-name" maxlength="120" placeholder="අලුත් නමක් දෙන්න — ගොනු අග කොටස ස්වයංක්‍රීයයි"><button class="button button-secondary">නම දුන් විගස යවන්න</button><button type="button" class="button button-quiet" id="pending-name-skip">මුල් නම</button></form><div class="pending-foot">නමක් දුන් විට Done බොත්තමක් අවශ්‍ය නැහැ; file එක වහාම යවයි.</div><div class="danger-actions"><button id="discard-output" class="button button-danger-quiet">ප්‍රතිදානය ඉවත් කරන්න</button></div>`;
    $('pending-name-form').addEventListener('submit',async e=>{e.preventDefault();try{const r=await post('/dashboard/api/name',{action:'set',name:$('pending-output-name').value});flash(`“${r.name}” නමින් යවමින්`);await loadState();}catch(err){flash(err.message,'error');}});
    $('pending-name-skip').addEventListener('click',async()=>{try{await post('/dashboard/api/name',{action:'skip'});flash('මුල් නමින් යවමින්');await loadState();}catch(e){flash(e.message,'error');}});
    $('discard-output').addEventListener('click',async()=>{if(!window.confirm('මෙම තාවකාලික ප්‍රතිදාන ගොනුව මකා දමන්නද?'))return;try{await post('/dashboard/api/delivery/cancel',{});flash('ප්‍රතිදාන ගොනුව ඉවත් කළා');await loadState();}catch(e){flash(e.message,'error');}});
  }
  function renderFiles(data){
    const video=data.video, sub=data.subtitle;
    const row=(icon,name,meta,good=true)=>`<div class="file-row"><span class="file-icon">${icon}</span><span class="file-info"><b title="${esc(name)}">${esc(name)}</b><small>${meta}</small></span><span class="file-status">${good?'● සූදානම්':'○ නොමැත'}</span></div>`;
    let html=''; if(video) html+=row('▶',video.name,`${bytes(video.size)} • ${video.media.width}×${video.media.height} • ${duration(video.media.duration)}`); else html+=row('▶','වීඩියෝවක් නැහැ','වීඩියෝව එක් කර සකස් කිරීම අරඹන්න',false);
    html+=row('▤',sub?.name||'උපසිරැසියක් නැහැ',sub?.ready?`${bytes(sub.size)} • SRT`:'Sinhala උපසිරැසිය එක් කරන්න',!!sub?.ready);
    $('current-files').innerHTML=html; $('library-detail').innerHTML=html;
    $('delete-video').disabled=!video; $('delete-subtitle').disabled=!sub?.ready;
    $('delete-subtitle').onclick=()=>deleteLibrary('subtitle','SRT උපසිරැසි ගොනුව ඉවත් කරන්නද?');
    $('delete-video').onclick=()=>deleteLibrary('all','වීඩියෝව සහ උපසිරැසි ගොනු ඉවත් කරන්නද?');
  }
  async function deleteLibrary(scope,question){if(!window.confirm(question))return;try{await post('/dashboard/api/delete',{scope});flash('ගොනු ඉවත් කළා');await loadState();}catch(e){flash(e.message,'error');}}
  function renderTracks(data){
    const area=$('tracks-area'); if(!data.video){area.innerHTML='<div class="empty-state">Video upload කළ පසු tracks තෝරාගත හැක.</div>';return;}
    const audio=data.video.media.audio||[], subs=data.video.media.subs||[]; const op=$('operation').value;
    let groups=[];
    if(op==='audio_extract'){
      if(audio.length)groups.push(`<div class="track-group"><h4>🎧 ලබාගත යුතු ශ්‍රව්‍ය පථය</h4><div class="track-checks">${audio.map((t,i)=>`<label class="track-chip"><input type="radio" name="extract-track" data-track-kind="audio" value="${t.index}" ${i===0?'checked':''}><span>${esc(t.lang)} · ${esc(t.codec)} · ${t.channels}ch</span></label>`).join('')}</div></div>`);
    }else if(op==='subtitle_extract'){
      if(subs.length)groups.push(`<div class="track-group"><h4>💬 වීඩියෝවේ ඇති ලබාගත හැකි උපසිරැසිය</h4><div class="track-checks">${subs.map((t,i)=>`<label class="track-chip"><input type="radio" name="extract-track" data-track-kind="subtitle" value="${t.index}" ${t.extractable?'':'disabled'} ${t.extractable&&subs.findIndex(x=>x.extractable)===i?'checked':''}><span>${esc(t.lang)} · ${esc(t.codec)}${t.title?' · '+esc(t.title):''}${t.extractable?'':' · ලබාගත නොහැක'}</span></label>`).join('')}</div></div>`);
    }else{
      if(audio.length) groups.push(`<div class="track-group"><h4>🔊 තබාගන්නා ශ්‍රව්‍ය පථ</h4><div class="track-checks">${audio.map(t=>`<label class="track-chip"><input type="checkbox" data-track-kind="audio" value="${t.index}" checked><span>${esc(t.lang)} · ${esc(t.codec)} · ${t.channels}ch</span></label>`).join('')}</div></div>`);
      if(op==='embed'&&subs.length) groups.push(`<div class="track-group"><h4>💬 වීඩියෝවට එක් කළ යුතු උපසිරැසි පථ</h4><div class="track-checks">${subs.map(t=>`<label class="track-chip"><input type="checkbox" data-track-kind="subtitle" value="${t.index}" checked><span>${esc(t.lang)} · ${esc(t.codec)}${t.title?' · '+esc(t.title):''}</span></label>`).join('')}</div></div>`);
    }
    area.innerHTML=groups.join('')||'<div class="empty-state">තෝරාගැනීමට මාධ්‍ය පථ නැත.</div>';
  }
  function updateOperationUI(){
    const op=$('operation').value, extracting=op==='audio_extract'||op==='subtitle_extract';
    $('mode-field').hidden=op!=='hardcode'; $('output-fields').hidden=extracting;
    $('time-fields').hidden=extracting||op==='embed'; $('watermark-field').hidden=op!=='hardcode';
    $('subtitle-drop').hidden=extracting;
    $('upload-desc').textContent=extracting?'වීඩියෝවේ ඇති පථ අතරින් අවශ්‍ය එක ලබාගන්න.':'වීඩියෝව සහ `.srt` උපසිරැසි ගොනුව එක් කරන්න.';
    $('start-job').innerHTML=op==='audio_extract'?'<span class="button-spark">✦</span> ශ්‍රව්‍ය පථය ලබාගන්න <span>→</span>':op==='subtitle_extract'?'<span class="button-spark">✦</span> උපසිරැසිය ලබාගන්න <span>→</span>':'<span class="button-spark">✦</span> සකස් කිරීම ආරම්භ කරන්න <span>→</span>';
    $('mode').disabled=!!state.data?.active_job||op!=='hardcode';
    $('delay-input').disabled=!!state.data?.active_job||extracting;
    $('name-input').disabled=!!state.data?.active_job||extracting;
  }
  function renderActivity(days){
    const data=[]; const map=new Map(days.map(x=>[x.day,x.count])); for(let i=6;i>=0;i--){const d=new Date();d.setDate(d.getDate()-i);const key=d.toLocaleDateString('en-CA');data.push({label:d.toLocaleDateString(undefined,{weekday:'short'}),count:map.get(key)||0});}
    const max=Math.max(1,...data.map(x=>x.count)); $('activity-chart').innerHTML=data.map(x=>`<div class="activity-col"><div class="bar-wrap"><i style="height:${Math.max(4,x.count/max*100)}%"></i></div><b>${x.count}</b><small>${esc(x.label)}</small></div>`).join('');
  }
  function renderHistory(items){
    const el=$('history-list'); if(!items.length){el.innerHTML='<div class="empty-state">තවම job history එකක් නැහැ. ඔබගේ පළමු job එක මෙතැනින් ආරම්භ කරන්න.</div>';return;}
    el.innerHTML=items.map(x=>{const status=x.status||'running';const icon=status==='completed'?'✓':status==='failed'?'!':status==='cancelled'?'×':'◷';const kind=x.kind==='hardcode'?'🔥 වීඩියෝවට උපසිරැසි දමීම':x.kind==='embed'?'🧩 උපසිරැසි එක් කිරීම':x.kind==='xa'?'🎧 ශ්‍රව්‍ය ලබාගැනීම':x.kind==='xs'?'💬 උපසිරැසි ලබාගැනීම':x.kind==='sync'?'🔄 උපසිරැසි ගැළපීම පරීක්ෂා කිරීම':esc(x.kind);const statusSi=status==='completed'?'සාර්ථකයි':status==='failed'?'දෝෂයක්':status==='cancelled'?'අවලංගු කළා':'ක්‍රියාත්මකයි';const name=x.output_name||x.input_name||'වීඩියෝ කාර්යය';return `<article class="history-item"><span class="history-icon ${esc(status)}">${icon}</span><div class="history-info"><b title="${esc(name)}">${esc(name)}</b><small>${kind} · ${statusSi} · ${dateText(x.started_at)}${x.error?' · '+esc(errorSi(x.error)):''}</small></div><div class="history-stats"><b>${x.output_size?bytes(x.output_size):'—'}</b>${x.elapsed?duration(x.elapsed):'—'}</div>${x.can_retry?`<div class="history-actions"><button class="button button-quiet retry-job" data-id="${esc(x.job_id)}">↻ නැවත උත්සාහ කරන්න</button></div>`:''}</article>`}).join('');
    el.querySelectorAll('.retry-job').forEach(btn=>btn.addEventListener('click',async()=>{if(!window.confirm('දැනට තිබෙන මූලාශ්‍ර වීඩියෝව සහ උපසිරැසි යොදා මෙම කාර්යය නැවත අරඹන්නද?'))return;try{await post('/dashboard/api/retry',{job_id:btn.dataset.id});flash('කාර්යය නැවත ආරම්භ කළා');await loadState();}catch(e){flash(e.message,'error');}}));
  }
  function colorValue(v, fallback){let s=String(v||fallback||'FFFFFF').replace(/^#|&H/gi,'');if(s.length>6)s=s.slice(-6);return `#${s.padStart(6,'0')}`;}
  function renderFonts(fonts,style){
    const select=$('font-select'); const current=style.font||''; const opts=fonts.map(f=>`<option value="${esc(f.family)}">${esc(f.family)}${f.owned?' • ඔබගේ':''}</option>`);
    if(current&&!fonts.some(f=>f.family===current))opts.unshift(`<option value="${esc(current)}">${esc(current)} • දැනට තෝරා ඇත</option>`);
    if(!fonts.length&&!current)opts.push('<option value="">සේවාදායකයේ පෙරනිමි අකුරු</option>');
    select.innerHTML=opts.join(''); select.value=current||select.options[0]?.value||'';
    $('font-list').innerHTML=fonts.length?fonts.map(f=>`<div class="font-item"><b title="${esc(f.family)}">${esc(f.family)}</b><small>${bytes(f.size)}</small>${f.owned?`<button class="button button-danger-quiet delete-font" data-name="${esc(f.name)}">මකන්න</button>`:''}</div>`).join(''):'<div class="empty-state">තවම ඔබ upload කළ font එකක් නැහැ.</div>';
    $('font-list').querySelectorAll('.delete-font').forEach(b=>b.addEventListener('click',async()=>{if(!window.confirm('ඔබ upload කළ මෙම font එක මකා දමන්නද?'))return;try{await post('/dashboard/api/font/delete',{name:b.dataset.name});flash('අකුරු ගොනුව ඉවත් කළා');await loadState();}catch(e){flash(e.message,'error');}}));
  }
  function applyStyle(s){$('font-size').value=s.size??52;$('alignment').value=s.alignment??2;$('primary-color').value=colorValue(s.primary,'FFFFFF');$('outline-color').value=colorValue(s.outline_c,'000000');$('outline-width').value=s.outline??2;$('shadow-width').value=s.shadow??1;$('margin-h').value=s.margin_h??30;$('margin-v').value=s.margin_v??24;$('spacing').value=s.spacing??0;$('bold').checked=!!s.bold;}

  function updateSyncUI(job){const labels={idle:'පරීක්ෂාවක් ආරම්භ කරන්න.',en:'දැන් English SRT ගොනුව upload කරන්න.',si:'English ගොනුව ලැබුණා. දැන් සිංහල SRT එක upload කරන්න.',running:'Sync analysis ක්‍රියාත්මකයි.',done:'Sync පරීක්ෂාව නිමයි. වාර්තාව Telegram chat එකට යවා ඇත.'};if(job?.kind==='sync')$('sync-status').textContent=`${job.stage||'Sync analysis'} • ${Math.round(Number(job.progress||0))}% • ගතවූ ${duration(job.elapsed)}${job.eta?` • ඉතිරි ${duration(job.eta)}`:''}`;else if(job?.kind==='sync_wait')$('sync-status').textContent=labels[state.syncStage];else $('sync-status').textContent=labels[state.syncStage]||labels.idle;$('sync-start').hidden=['en','si','running'].includes(state.syncStage);$('sync-file').disabled=!['en','si'].includes(state.syncStage);$('sync-upload').disabled=!state.syncFile||!['en','si'].includes(state.syncStage);$('sync-upload').hidden=!['en','si'].includes(state.syncStage);$('sync-cancel').hidden=!['en','si','running'].includes(state.syncStage);$('sync-file-title').textContent=state.syncStage==='si'?'සිංහල SRT තෝරන්න':'English SRT තෝරන්න';$('sync-file-name').textContent=state.syncFile?.name||(state.syncStage==='si'?'English file සූදානම් • සිංහල file එක තෝරන්න':'English පළමුව; ඉන්පසු සිංහල');}
  $('sync-file').addEventListener('change',e=>{state.syncFile=e.target.files[0]||null;updateSyncUI(state.data?.active_job);});
  $('sync-start').addEventListener('click',async()=>{try{await post('/dashboard/api/sync/start',{});state.syncStage='en';state.syncFile=null;setFeedback('sync-feedback','Sync workflow ආරම්භ කළා.','ok');updateSyncUI();await loadState();}catch(e){setFeedback('sync-feedback',e.message,'error');}});
  $('sync-upload').addEventListener('click',async()=>{if(!state.syncFile)return;const file=state.syncFile,form=new FormData();form.append('file',file,file.name);$('sync-upload').disabled=true;setFeedback('sync-feedback','SRT එක upload කර විශ්ලේෂණය කරමින්…');try{const result=await api('/dashboard/api/sync/upload',{method:'POST',body:form});state.syncFile=null;$('sync-file').value='';state.syncStage=result.stage;setFeedback('sync-feedback',result.stage==='si'?`English SRT ලැබුණා • ${result.cues} cues`:'Sync analysis ආරම්භ කළා.','ok');await loadState();}catch(e){setFeedback('sync-feedback',e.message,'error');updateSyncUI(state.data?.active_job);}});
  $('sync-cancel').addEventListener('click',async()=>{try{await post('/dashboard/api/cancel',{});state.syncStage='idle';state.syncFile=null;$('sync-file').value='';setFeedback('sync-feedback','Sync පරීක්ෂාව නවතා දැමුවා.','ok');await loadState();}catch(e){setFeedback('sync-feedback',e.message,'error');}});
  function selectedTracks(kind){return [...document.querySelectorAll(`[data-track-kind="${kind}"]:checked`)].map(x=>Number(x.value));}
  $('style-form').addEventListener('input',()=>{state.styleDirty=true;renderPreviewCue();});
  $('style-form').addEventListener('change',()=>{state.styleDirty=true;renderPreviewCue();});
  $('watermark-text').addEventListener('input',()=>{state.watermarkDirty=true;});
  $('operation').addEventListener('change',()=>{if(state.data)renderTracks(state.data);updateOperationUI();if(state.data)render(state.data);});
  $('full-video').addEventListener('change',()=>{document.querySelector('.time-inputs').hidden=$('full-video').checked;});
  $('video-file').addEventListener('change',e=>{state.pendingVideo=e.target.files[0]||null;$('video-choice').textContent=state.pendingVideo?.name||'MP4, MKV, MOV සහ තවත්';if(state.pendingVideo)setLocalVideo(state.pendingVideo);$('upload-selected').disabled=state.busy||(!state.pendingVideo&&!state.pendingSrt);});
  $('subtitle-file').addEventListener('change',e=>{state.pendingSrt=e.target.files[0]||null;$('subtitle-choice').textContent=state.pendingSrt?.name||'Unicode Sinhala සහාය';if(state.pendingSrt)setLocalSrt(state.pendingSrt);$('upload-selected').disabled=state.busy||(!state.pendingVideo&&!state.pendingSrt);});
  $('local-preview').addEventListener('timeupdate',renderPreviewCue);
  $('local-preview').addEventListener('error',()=>{if(state.pendingVideo)flash('මෙම browser එකේ video preview format එක play කළ නොහැක. Upload සහ server processing වෙනම උත්සාහ කරන්න.','error');});
  for(const id of ['video-drop','subtitle-drop']){const el=$(id);el.addEventListener('dragover',e=>{e.preventDefault();el.classList.add('drag')});el.addEventListener('dragleave',()=>el.classList.remove('drag'));el.addEventListener('drop',e=>{e.preventDefault();el.classList.remove('drag');const f=e.dataTransfer.files[0];if(!f)return;if(id==='video-drop'){state.pendingVideo=f;$('video-choice').textContent=f.name;setLocalVideo(f);}else{state.pendingSrt=f;$('subtitle-choice').textContent=f.name;setLocalSrt(f);}$('upload-selected').disabled=false;});}
  function uploadFile(file,kind){return new Promise((resolve,reject)=>{const form=new FormData();form.append('file',file,file.name);const xhr=new XMLHttpRequest();xhr.open('POST','/dashboard/api/upload');xhr.setRequestHeader('X-Telegram-Init-Data',initData);xhr.upload.onprogress=e=>{if(e.lengthComputable){$('upload-progress').hidden=false;$('upload-label').textContent=`${kind==='video'?'Video':'SRT'} upload: ${Math.round(e.loaded/e.total*100)}%`;$('upload-bar').style.width=`${e.loaded/e.total*100}%`;}};xhr.onerror=()=>reject(new Error('Network error'));xhr.onload=()=>{let data={};try{data=JSON.parse(xhr.responseText)}catch(_){};if(xhr.status>=200&&xhr.status<300)resolve(data);else reject(new Error(data.message||`Upload failed (${xhr.status})`));};xhr.send(form);});}
  $('upload-selected').addEventListener('click',async()=>{if(state.busy)return;state.busy=true;$('upload-selected').disabled=true;setFeedback('upload-feedback','ගොනු එක් කරමින්…');try{if(state.pendingVideo){const r=await uploadFile(state.pendingVideo,'video');flash(`වීඩියෝව ලැබුණා: ${r.name}`);state.pendingVideo=null;$('video-file').value='';$('video-choice').textContent='MP4, MKV, MOV සහ තවත්';}if(state.pendingSrt){const r=await uploadFile(state.pendingSrt,'subtitle');flash(`SRT ලැබුණා • ${r.cues||0} cues`);state.pendingSrt=null;$('subtitle-file').value='';$('subtitle-choice').textContent='Unicode Sinhala සහාය';}setFeedback('upload-feedback','ගොනු සූදානම්. දැන් සකස් කිරීම අරඹන්න.','ok');}catch(e){setFeedback('upload-feedback',e.message,'error');}finally{state.busy=false;$('upload-progress').hidden=true;$('upload-bar').style.width='0';await loadState();}});
  $('process-form').addEventListener('submit',async e=>{e.preventDefault();if($('start-job').disabled)return;const kind=$('operation').value;const payload={kind};if(kind==='audio_extract'||kind==='subtitle_extract'){const track=document.querySelector(`[data-track-kind="${kind==='audio_extract'?'audio':'subtitle'}"]:checked`);if(!track){flash('ලබාගත යුතු track එකක් තෝරන්න','error');return;}payload.track_index=Number(track.value);}else{Object.assign(payload,{mode:$('mode').value,watermark:$('watermark').checked,delay_ms:Number($('delay-input').value||0),audio_tracks:selectedTracks('audio'),subtitle_tracks:selectedTracks('subtitle'),output_name:$('name-input').value.trim(),full:$('full-video').checked,start_seconds:Number($('start-time').value||0),end_seconds:Number($('end-time').value||0)});}$('start-job').disabled=true;try{await post('/dashboard/api/start',payload);flash('Job එක ආරම්භ කළා');$('name-input').value='';await loadState();}catch(err){flash(err.message,'error');await loadState();}});
  $('style-form').addEventListener('submit',async e=>{e.preventDefault();const style={font:$('font-select').value,size:Number($('font-size').value),alignment:Number($('alignment').value),primary:$('primary-color').value.slice(1),outline_c:$('outline-color').value.slice(1),outline:Number($('outline-width').value),shadow:Number($('shadow-width').value),margin_h:Number($('margin-h').value),margin_v:Number($('margin-v').value),spacing:Number($('spacing').value),bold:$('bold').checked};try{await post('/dashboard/api/style',{style});state.styleDirty=false;setFeedback('style-feedback','Subtitle style සුරැකුවා.','ok');flash('Style සුරැකුවා');await loadState();}catch(err){setFeedback('style-feedback',err.message,'error');}});
  $('preview-style').addEventListener('click',async()=>{try{const response=await fetch('/dashboard/api/style/preview',{method:'POST',headers:{'X-Telegram-Init-Data':initData}});if(!response.ok){let d={};try{d=await response.json()}catch(_){};throw new Error(d.message||'Preview නොලැබුණි');}const blob=await response.blob();const img=$('style-preview');if(img.dataset.url)URL.revokeObjectURL(img.dataset.url);img.dataset.url=URL.createObjectURL(blob);img.src=img.dataset.url;img.hidden=false;setFeedback('style-feedback','Subtitle preview සූදානම්.','ok');}catch(err){setFeedback('style-feedback',err.message,'error');}});
  $('watermark-form').addEventListener('submit',async e=>{e.preventDefault();try{const r=await post('/dashboard/api/watermark',{text:$('watermark-text').value});state.watermarkDirty=false;setFeedback('watermark-feedback',`Watermark සුරැකුවා: ${r.watermark}`,'ok');flash('Watermark සුරැකුවා');}catch(err){setFeedback('watermark-feedback',err.message,'error');}});
  $('font-upload').addEventListener('click',async()=>{const file=$('font-file').files[0];if(!file){flash('Font file එකක් තෝරන්න','error');return;}const form=new FormData();form.append('file',file,file.name);try{const r=await api('/dashboard/api/font',{method:'POST',body:form});flash(`Font එක් කළා: ${r.font.family}`);$('font-file').value='';await loadState();}catch(e){flash(e.message,'error');}});
  $('refresh').addEventListener('click',loadState);
  setupTabs();
  if(!initData){showGate();}else{loadState();state.poll=setInterval(()=>{if(!state.busy&&!document.hidden)loadState();},1000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadState();});}
})();
