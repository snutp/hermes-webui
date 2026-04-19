async function cancelStream(){
  const streamId = S.activeStreamId;
  if(!streamId) return;
  try{
    await fetch(new URL(`/api/chat/cancel?stream_id=${encodeURIComponent(streamId)}`,location.origin).href,{credentials:'include'});
  }catch(e){/* cancel request failed — cleanup below still runs */}
  // Clear status unconditionally after the cancel request completes.
  // The SSE cancel event may also fire, but if the connection is already
  // closed it won't arrive — so we handle cleanup here as the guaranteed path.
  const btn=$('btnCancel');if(btn)btn.style.display='none';
  S.activeStreamId=null;
  setBusy(false);
  if(typeof setComposerStatus==='function') setComposerStatus('');
  else setStatus('');
}

// ── Mobile navigation ──────────────────────────────────────────────────────
let _workspacePanelMode='closed'; // 'closed' | 'browse' | 'preview'

function _isCompactWorkspaceViewport(){
  return window.matchMedia('(max-width: 900px)').matches;
}

function _workspacePanelEls(){
  return {
    layout: document.querySelector('.layout'),
    panel: document.querySelector('.rightpanel'),
    toggleBtn: $('btnWorkspacePanelToggle'),
    collapseBtn: $('btnCollapseWorkspacePanel'),
  };
}

function _hasWorkspacePreviewVisible(){
  const preview=$('previewArea');
  return !!(preview&&preview.classList.contains('visible'));
}

function _setWorkspacePanelMode(mode){
  const {layout,panel}= _workspacePanelEls();
  if(!layout||!panel)return;
  _workspacePanelMode=(mode==='browse'||mode==='preview')?mode:'closed';
  const open=_workspacePanelMode!=='closed';
  // Persist open/closed across refreshes (browse/preview → open; closed → closed)
  localStorage.setItem('hermes-webui-workspace-panel', open ? 'open' : 'closed');
  layout.classList.toggle('workspace-panel-collapsed',!open);
  if(_isCompactWorkspaceViewport()){
    panel.classList.toggle('mobile-open',open);
  }else{
    panel.classList.remove('mobile-open');
  }
  syncWorkspacePanelUI();
}

function syncWorkspacePanelState(){
  const hasPreview=_hasWorkspacePreviewVisible();
  if(hasPreview){
    if(_workspacePanelMode==='closed') _setWorkspacePanelMode('preview');
    else syncWorkspacePanelUI();
    return;
  }
  if(!S.session){
    _setWorkspacePanelMode('closed');
    return;
  }
  _setWorkspacePanelMode(_workspacePanelMode==='preview'?'closed':_workspacePanelMode);
}

function openWorkspacePanel(mode='browse'){
  if(mode==='browse'&&!S.session&&!_hasWorkspacePreviewVisible())return;
  if(mode==='preview'&&_workspacePanelMode==='browse'){
    syncWorkspacePanelUI();
    return;
  }
  _setWorkspacePanelMode(mode);
}

function closeWorkspacePanel(){
  _setWorkspacePanelMode('closed');
}

function ensureWorkspacePreviewVisible(){
  if(_workspacePanelMode==='closed') _setWorkspacePanelMode('preview');
  else syncWorkspacePanelUI();
}

function handleWorkspaceClose(){
  if(_hasWorkspacePreviewVisible()){
    clearPreview();
    return;
  }
  closeWorkspacePanel();
}

function syncWorkspacePanelUI(){
  const {layout,panel,toggleBtn,collapseBtn}= _workspacePanelEls();
  if(!layout||!panel)return;
  const desktopOpen=_workspacePanelMode!=='closed';
  const mobileOpen=panel.classList.contains('mobile-open');
  const isCompact=_isCompactWorkspaceViewport();
  const isOpen=isCompact?mobileOpen:desktopOpen;
  const canBrowse=!!S.session||_hasWorkspacePreviewVisible();
  const hasPreview=_hasWorkspacePreviewVisible();
  if(toggleBtn){
    toggleBtn.classList.toggle('active',isOpen);
    toggleBtn.setAttribute('aria-pressed',isOpen?'true':'false');
    toggleBtn.title=isOpen?'Hide workspace panel':'Show workspace panel';
    toggleBtn.disabled=!canBrowse;
  }
  if(collapseBtn){
    collapseBtn.title=isCompact?'Close workspace panel':'Hide workspace panel';
  }
  const hasSession=!!S.session;
  ['btnUpDir','btnNewFile','btnNewFolder','btnRefreshPanel'].forEach(id=>{
    const el=$(id);
    if(el)el.disabled=!hasSession;
  });
  const clearBtn=$('btnClearPreview');
  if(clearBtn){
    clearBtn.disabled=!isOpen;
    clearBtn.title=hasPreview?'Close preview':'Hide workspace panel';
  }
}

function toggleMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  const overlay=$('mobileOverlay');
  if(!sidebar)return;
  const isOpen=sidebar.classList.contains('mobile-open');
  if(isOpen){closeMobileSidebar();}
  else{sidebar.classList.add('mobile-open');if(overlay)overlay.classList.add('visible');}
}
function closeMobileSidebar(){
  const sidebar=document.querySelector('.sidebar');
  const overlay=$('mobileOverlay');
  if(sidebar)sidebar.classList.remove('mobile-open');
  if(overlay)overlay.classList.remove('visible');
}
function toggleMobileFiles(){
  toggleWorkspacePanel();
}
function toggleWorkspacePanel(force){
  const {panel}= _workspacePanelEls();
  if(!panel)return;
  const currentlyOpen=_workspacePanelMode!=='closed';
  const nextOpen=typeof force==='boolean'?force:!currentlyOpen;
  if(!nextOpen){
    closeWorkspacePanel();
    return;
  }
  const nextMode=_hasWorkspacePreviewVisible()?'preview':'browse';
  openWorkspacePanel(nextMode);
}
function mobileSwitchPanel(name){
  // Switch the panel content view
  switchPanel(name);
  // For non-chat panels (tasks, skills, memory, spaces), open the sidebar
  // so the panel is visible. For 'chat', the content is in the main area —
  // just close the sidebar so the chat view is unobstructed.
  if(name==='chat'){
    closeMobileSidebar();
  } else {
    const sidebar=document.querySelector('.sidebar');
    const overlay=$('mobileOverlay');
    if(sidebar){
      sidebar.classList.add('mobile-open');
      if(overlay)overlay.classList.add('visible');
    }
  }
  // Update bottom nav active state
  document.querySelectorAll('.mobile-nav-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.panel===name);
  });
}

$('btnSend').onclick=()=>{
  if(window._micActive){
    window._micPendingSend=true;
    _stopMic();
    return;
  }
  send();
};
$('btnAttach').onclick=()=>$('fileInput').click();

// ── Voice input (Web Speech API + MediaRecorder fallback) ───────────────────
(function(){
  const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const _canRecordAudio=!!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.MediaRecorder);
  if(!SpeechRecognition&&!_canRecordAudio) return; // Browser unsupported — mic button stays hidden

  const btn=$('btnMic');
  const status=$('micStatus');
  const ta=$('msg');
  const statusText=status?status.querySelector('.status-text'):null;
  btn.style.display=''; // Show button — browser supports speech recognition or recording fallback

  let recognition=SpeechRecognition?new SpeechRecognition():null;
  let mediaRecorder=null;
  let mediaStream=null;
  let audioChunks=[];
  let _finalText='';
  let _prefix='';

  function _setRecording(on){
    window._micActive=on;
    btn.classList.toggle('recording',on);
    status.style.display=on?'':'none';
    if(statusText) statusText.textContent=on?'Listening':'Listening';
    if(!on){ _finalText=''; _prefix=''; }
  }

  function _commitTranscript(text){
    const clean=(text||'').trim();
    const committed=clean
      ? (_prefix&&!_prefix.endsWith(' ')&&!_prefix.endsWith('\n')
          ? _prefix+' '+clean.trimStart()
          : _prefix+clean)
      : ta.value;
    ta.value=committed;
    autoResize();
    if(window._micPendingSend){
      window._micPendingSend=false;
      send();
    }
  }

  async function _transcribeBlob(blob){
    const ext=(blob.type&&blob.type.includes('ogg'))?'ogg':'webm';
    const form=new FormData();
    form.append('file',new File([blob],`voice-input.${ext}`,{type:blob.type||`audio/${ext}`}));
    setComposerStatus('Transcribing…');
    try{
      const res=await fetch('/api/transcribe',{method:'POST',body:form});
      const data=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(data.error||'Transcription failed');
      _commitTranscript(data.transcript||'');
    }catch(err){
      window._micPendingSend=false;
      showToast(err.message||t('mic_network'));
    }finally{
      setComposerStatus('');
    }
  }

  function _stopTracks(){
    if(mediaStream){
      mediaStream.getTracks().forEach(track=>track.stop());
      mediaStream=null;
    }
  }

  function _stopMic(){
    if(!window._micActive) return;
    if(recognition){
      recognition.stop();
      return;
    }
    if(mediaRecorder&&mediaRecorder.state!=='inactive'){
      mediaRecorder.stop();
      return;
    }
    _setRecording(false);
    _stopTracks();
  }
  window._stopMic=_stopMic; // expose for send-guard above

  if(recognition){
    recognition.continuous=false;
    recognition.interimResults=true;
    recognition.lang=(typeof _locale!=='undefined'&&_locale._speech)||'en-US';

    recognition.onstart=()=>{ _finalText=''; };

    recognition.onresult=(event)=>{
      let interim='';
      let final=_finalText;
      for(let i=event.resultIndex;i<event.results.length;i++){
        const t=event.results[i][0].transcript;
        if(event.results[i].isFinal){ final+=t; _finalText=final; }
        else{ interim+=t; }
      }
      ta.value=_prefix+(final||interim);
      autoResize();
    };

    recognition.onend=()=>{
      const committed=_finalText
        ? (_prefix&&!_prefix.endsWith(' ')&&!_prefix.endsWith('\n')
            ? _prefix+' '+_finalText.trimStart()
            : _prefix+_finalText)
        : ta.value;
      _setRecording(false);
      ta.value=committed;
      autoResize();
      if(window._micPendingSend){
        window._micPendingSend=false;
        send();
      }
    };

    recognition.onerror=(event)=>{
      _setRecording(false);
      window._micPendingSend=false;
      const msgs={
        'not-allowed':t('mic_denied'),
        'no-speech':t('mic_no_speech'),
        'network':t('mic_network'),
      };
      showToast(msgs[event.error]||t('mic_error')+event.error);
    };
  }

  btn.onclick=async()=>{
    if(window._micActive){
      _stopMic();
      return;
    }
    _finalText='';
    _prefix=ta.value;
    if(recognition){
      recognition.start();
      _setRecording(true);
      return;
    }
    if(!_canRecordAudio){
      showToast(t('mic_network'));
      return;
    }
    try{
      mediaStream=await navigator.mediaDevices.getUserMedia({audio:true});
      const preferredTypes=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/ogg'];
      const mimeType=preferredTypes.find(type=>window.MediaRecorder.isTypeSupported?.(type))||'';
      mediaRecorder=new MediaRecorder(mediaStream,mimeType?{mimeType}:undefined);
      audioChunks=[];
      mediaRecorder.ondataavailable=e=>{if(e.data&&e.data.size)audioChunks.push(e.data);};
      mediaRecorder.onerror=()=>{
        _setRecording(false);
        window._micPendingSend=false;
        _stopTracks();
        showToast(t('mic_network'));
      };
      mediaRecorder.onstop=async()=>{
        const blob=new Blob(audioChunks,{type:mediaRecorder.mimeType||mimeType||'audio/webm'});
        _setRecording(false);
        _stopTracks();
        if(blob.size){ await _transcribeBlob(blob); }
        else if(window._micPendingSend){
          window._micPendingSend=false;
        }
      };
      mediaRecorder.start();
      _setRecording(true);
    }catch(err){
      window._micPendingSend=false;
      _stopTracks();
      showToast(t('mic_denied'));
    }
  };
})();
window._micActive=window._micActive||false;
window._micPendingSend=window._micPendingSend||false;
$('fileInput').onchange=e=>{addFiles(Array.from(e.target.files));e.target.value='';};
$('btnNewChat').onclick=async()=>{await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();};
$('btnDownload').onclick=()=>{
  if(!S.session)return;
  const blob=new Blob([transcript()],{type:'text/markdown'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=`hermes-${S.session.session_id}.md`;a.click();URL.revokeObjectURL(a.href);
};
$('btnExportJSON').onclick=()=>{
  if(!S.session)return;
  const url=`/api/session/export?session_id=${encodeURIComponent(S.session.session_id)}`;
  const a=document.createElement('a');a.href=url;
  a.download=`hermes-${S.session.session_id}.json`;a.click();
};
$('btnImportJSON').onclick=()=>$('importFileInput').click();
$('importFileInput').onchange=async(e)=>{
  const file=e.target.files[0];
  if(!file)return;
  e.target.value='';
  try{
    const text=await file.text();
    const data=JSON.parse(text);
    const res=await api('/api/session/import',{method:'POST',body:JSON.stringify(data)});
    if(res.ok&&res.session){
      await loadSession(res.session.session_id);
      await renderSessionList();
      const overlay=$('settingsOverlay');
      if(overlay) overlay.style.display='none';
      showToast(t('session_imported'));
    }
  }catch(err){
    showToast(t('import_failed')+(err.message||t('import_invalid_json')));
  }
};
// btnRefreshFiles is now panel-icon-btn in header (see HTML)
function clearPreview(){
  const closePanelAfter=_workspacePanelMode==='preview';
  const pa=$('previewArea');if(pa)pa.classList.remove('visible');
  const pi=$('previewImg');if(pi){pi.onerror=null;pi.src='';}
  const pm=$('previewMd');if(pm)pm.innerHTML='';
  const pc=$('previewCode');if(pc)pc.textContent='';
  const pp=$('previewPathText');if(pp)pp.textContent='';
  const ft=$('fileTree');if(ft)ft.style.display='';
  _previewCurrentPath='';_previewCurrentMode='';_previewDirty=false;
  // Restore directory breadcrumb after closing file preview
  if(typeof renderBreadcrumb==='function') renderBreadcrumb();
  if(closePanelAfter)closeWorkspacePanel();
  else syncWorkspacePanelUI();
}
$('btnClearPreview').onclick=handleWorkspaceClose;
// workspacePath click handler removed -- use topbar workspace chip dropdown instead
$('modelSelect').onchange=async()=>{
  if(!S.session)return;
  const selectedModel=$('modelSelect').value;
  if(typeof closeModelDropdown==='function') closeModelDropdown();
  localStorage.setItem('hermes-webui-model', selectedModel);
  await api('/api/session/update',{method:'POST',body:JSON.stringify({session_id:S.session.session_id,workspace:S.session.workspace,model:selectedModel})});
  S.session.model=selectedModel;
  if(typeof syncModelChip==='function') syncModelChip();
  syncTopbar();
  // Warn if selected model belongs to a different provider than what Hermes is configured for
  if(typeof _checkProviderMismatch==='function'){
    const warn=_checkProviderMismatch(selectedModel);
    if(warn&&typeof showToast==='function') showToast(warn,4000);
  }
};
$('msg').addEventListener('input',()=>{
  autoResize();
  updateSendBtn();
  const text=$('msg').value;
  if(text.startsWith('/')&&text.indexOf('\n')===-1){
    const prefix=text.slice(1);
    const matches=getMatchingCommands(prefix);
    if(matches.length)showCmdDropdown(matches); else hideCmdDropdown();
  } else {
    hideCmdDropdown();
  }
});
$('msg').addEventListener('keydown',e=>{
  // Autocomplete navigation when dropdown is open
  const dd=$('cmdDropdown');
  const dropdownOpen=dd&&dd.classList.contains('open');
  if(dropdownOpen){
    if(e.key==='ArrowUp'){e.preventDefault();navigateCmdDropdown(-1);return;}
    if(e.key==='ArrowDown'){e.preventDefault();navigateCmdDropdown(1);return;}
    if(e.key==='Tab'){e.preventDefault();selectCmdDropdownItem();return;}
    if(e.key==='Escape'){e.preventDefault();hideCmdDropdown();return;}
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();selectCmdDropdownItem();return;}
  }
  // Send key: respect user preference.
  // On touch-primary devices (software keyboard), default to Enter = newline
  // since there's no physical Shift key. Users send via the Send button.
  // The 'ctrl+enter' setting also uses this behavior (Enter = newline).
  // Users can override in Settings by explicitly choosing 'enter' mode.
  if(e.key==='Enter'){
    const _mobileDefault=matchMedia('(pointer:coarse)').matches&&window._sendKey==='enter';
    if(window._sendKey==='ctrl+enter'||_mobileDefault){
      if(e.ctrlKey||e.metaKey){e.preventDefault();send();}
    } else {
      if(!e.shiftKey){e.preventDefault();send();}
    }
  }
});
// B14: Cmd/Ctrl+K creates a new chat from anywhere
document.addEventListener('keydown',async e=>{
  // Enter on approval card = Allow once (when a button inside the card is focused or
  // card is visible and focus is not on an input/textarea/select)
  if(e.key==='Enter'&&!e.metaKey&&!e.ctrlKey&&!e.shiftKey){
    const card=$('approvalCard');
    const tag=(document.activeElement||{}).tagName||'';
    if(card&&card.classList.contains('visible')&&tag!=='TEXTAREA'&&tag!=='INPUT'&&tag!=='SELECT'){
      e.preventDefault();
      if(typeof respondApproval==='function') respondApproval('once');
      return;
    }
  }
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){
    e.preventDefault();
    if(!S.busy){await newSession();await renderSessionList();closeMobileSidebar();$('msg').focus();}
  }
  if(e.key==='Escape'){
    // Close settings overlay if open
    const settingsOverlay=$('settingsOverlay');
    if(settingsOverlay&&settingsOverlay.style.display!=='none'){_closeSettingsPanel();return;}
    // Close workspace dropdown
    closeWsDropdown();
    // Clear session search
    const ss=$('sessionSearch');
    if(ss&&ss.value){ss.value='';filterSessions();}
    // Cancel any active message edit
    const editArea=document.querySelector('.msg-edit-area');
    if(editArea){
      const bar=editArea.closest('.msg-row')&&editArea.closest('.msg-row').querySelector('.msg-edit-bar');
      if(bar){const cancel=bar.querySelector('.msg-edit-cancel');if(cancel)cancel.click();}
    }
  }
});
$('msg').addEventListener('paste',e=>{
  const items=Array.from(e.clipboardData?.items||[]);
  const imageItems=items.filter(i=>i.type.startsWith('image/'));
  if(!imageItems.length)return;
  e.preventDefault();
  const files=imageItems.map(i=>{
    const blob=i.getAsFile();
    const ext=i.type.split('/')[1]||'png';
    return new File([blob],`screenshot-${Date.now()}.${ext}`,{type:i.type});
  });
  addFiles(files);
  setStatus(t('image_pasted')+files.map(f=>f.name).join(', '));
});
/* ── Quick-start suggestions (dynamic) ── */
const SUGGESTION_DEFAULTS=[
  {icon:'folder',msg:'현재 프로젝트 상태 알려줘',label:'프로젝트 현황'},
  {icon:'upload',msg:'기획 관련 자료를 분석해줘',label:'자료 분석'},
  {icon:'layout',msg:'디자인 목업 생성해줘',label:'디자인 목업'},
  {icon:'file-text',msg:'제안서 작성해줘',label:'제안서 작성'},
  {icon:'code',msg:'프로토타입 빌드 시작해줘',label:'빌드 시작'},
];
const SUGGESTION_ICONS={
  'folder':'<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  'upload':'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
  'layout':'<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/>',
  'file-text':'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
  'code':'<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  'play':'<polygon points="5 3 19 12 5 21 5 3"/>',
  'rocket':'<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 3 0 3 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-3 0-3"/>',
  'zap':'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  'search':'<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  'clipboard':'<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/>',
};
function renderSuggestionBtn(s){
  const ic=SUGGESTION_ICONS[s.icon]||SUGGESTION_ICONS['folder'];
  const btn=document.createElement('button');
  btn.className='suggestion';
  btn.dataset.msg=s.msg;
  btn.innerHTML=`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ic}</svg> <span>${s.label}</span>`;
  btn.onclick=()=>{$('msg').value=btn.dataset.msg;send();};
  return btn;
}
function renderSuggestions(items,statusText){
  const grid=$('suggestionGrid');
  if(!grid)return;
  grid.innerHTML='';
  if(statusText){
    const badge=document.createElement('div');
    badge.className='suggestion-status';
    badge.textContent=statusText;
    grid.appendChild(badge);
  }
  items.forEach(s=>grid.appendChild(renderSuggestionBtn(s)));
}
(async()=>{
  try{
    const r=await fetch('/api/quick-start');
    if(r.ok){
      const d=await r.json();
      renderSuggestions(d.suggestions||SUGGESTION_DEFAULTS,d.status_label||null);
      return;
    }
  }catch(e){}
  renderSuggestions(SUGGESTION_DEFAULTS,null);
})();

window.addEventListener('resize',()=>{
  syncWorkspacePanelState();
});

// Boot: restore last session or start fresh
// ── Resizable panels ──────────────────────────────────────────────────────
(function(){
  const SIDEBAR_MIN=180, SIDEBAR_MAX=420;
  const PANEL_MIN=180,   PANEL_MAX=1200;

  function initResize(handleId, targetEl, edge, minW, maxW, storageKey){
    const handle = $(handleId);
    if(!handle || !targetEl) return;

    // Restore saved width
    const saved = localStorage.getItem(storageKey);
    if(saved) targetEl.style.width = saved + 'px';

    let startX=0, startW=0;

    handle.addEventListener('mousedown', e=>{
      e.preventDefault();
      startX = e.clientX;
      startW = targetEl.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.classList.add('resizing');

      const onMove = ev=>{
        const delta = edge==='right' ? ev.clientX - startX : startX - ev.clientX;
        const newW = Math.min(maxW, Math.max(minW, startW + delta));
        targetEl.style.width = newW + 'px';
      };
      const onUp = ()=>{
        handle.classList.remove('dragging');
        document.body.classList.remove('resizing');
        localStorage.setItem(storageKey, parseInt(targetEl.style.width));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  // Run after DOM ready (called from boot)
  window._initResizePanels = function(){
    const sidebar    = document.querySelector('.sidebar');
    const rightpanel = document.querySelector('.rightpanel');
    initResize('sidebarResize',    sidebar,    'right', SIDEBAR_MIN, SIDEBAR_MAX, 'hermes-sidebar-w');
    initResize('rightpanelResize', rightpanel, 'left',  PANEL_MIN,   PANEL_MAX,   'hermes-panel-w');
  };
})();

function applyBotName(){
  const name=window._botName||'Hermes';
  document.title=name;
  const sidebarH1=document.querySelector('.sidebar-header h1');
  if(sidebarH1) sidebarH1.textContent=name;
  const logo=document.querySelector('.sidebar-header .logo');
  if(logo) logo.textContent=name.charAt(0).toUpperCase();
  const topbarTitle=$('topbarTitle');
  if(topbarTitle && (!S.session)) topbarTitle.textContent=name;
  const msg=$('msg');
  if(msg) msg.placeholder='Message '+name+'\u2026';
}

(async()=>{
  // Load send key preference
  let _bootSettings={};
  try{const s=await api('/api/settings');_bootSettings=s;window._sendKey=s.send_key||'enter';window._showTokenUsage=!!s.show_token_usage;window._showCliSessions=!!s.show_cli_sessions;window._soundEnabled=!!s.sound_enabled;window._notificationsEnabled=!!s.notifications_enabled;window._botName=s.bot_name||'Hermes';const _theme=s.theme||'dark';document.documentElement.dataset.theme=_theme;localStorage.setItem('hermes-theme',_theme);document.body.classList.toggle('bubble-layout',!!s.bubble_layout);if(s.language&&typeof setLocale==='function'){setLocale(s.language);if(typeof applyLocaleToDOM==='function')applyLocaleToDOM();}applyBotName();}catch(e){window._sendKey='enter';window._showTokenUsage=false;window._showCliSessions=false;window._soundEnabled=false;window._notificationsEnabled=false;window._botName='Hermes';_bootSettings={check_for_updates:false};document.body.classList.remove('bubble-layout');}
  // Non-blocking update check (fire-and-forget, once per tab session)
  // ?test_updates=1 in URL forces banner display for testing (bypasses sessionStorage guards)
  const _testUpdates=new URLSearchParams(location.search).get('test_updates')==='1';
  if(_testUpdates||(_bootSettings.check_for_updates!==false&&!sessionStorage.getItem('hermes-update-checked')&&!sessionStorage.getItem('hermes-update-dismissed'))){
    const _checkUrl='/api/updates/check'+(_testUpdates?'?simulate=1':'');
    api(_checkUrl).then(d=>{if(!_testUpdates)sessionStorage.setItem('hermes-update-checked','1');if((d.webui&&d.webui.behind>0)||(d.agent&&d.agent.behind>0))_showUpdateBanner(d);}).catch(()=>{});
  }
  // Fetch active profile
  try{const p=await api('/api/profile/active');S.activeProfile=p.name||'default';}catch(e){S.activeProfile='default';}
  // Update profile chip label immediately
  const profileLabel=$('profileChipLabel');
  if(profileLabel) profileLabel.textContent=S.activeProfile||'default';
  // Phase 5: persist "last seen" context for the agent-offline.html fallback.
  // When portal reaps this webui and the browser hits a 502, that page reads
  // localStorage['webui:last:<port>'] to show when the user was last here
  // and which project to restart. Per-port key so multi-tab / project-switch
  // doesn't resurrect the wrong webui (Codex review blocker #2).
  (function _initLastSeen(){
    function save(){
      try{
        const portMatch = location.pathname.match(/\/agent\/(\d+)/);
        const port = portMatch ? portMatch[1] : null;
        if(!port) return;
        const profile = (S && S.activeProfile) || 'default';
        // profile naming convention from portal: "portal-<code>" (lowercase).
        // Portal URLs use the code in uppercase; normalise so the offline
        // page can round-trip to /projects/<CODE>.
        const m = profile.match(/^portal-(.+)$/i);
        const codeRaw = m ? m[1] : null;
        const projectCode = codeRaw ? codeRaw.toUpperCase() : null;
        const payload = JSON.stringify({
          ts: Date.now(), port, profile, projectCode,
        });
        // Per-port key (primary) + legacy global key (back-compat for offline
        // pages served before this change rolled out).
        localStorage.setItem('webui:last:' + port, payload);
        localStorage.setItem('webui:last', payload);
      }catch(e){ /* localStorage full / blocked — safe to ignore */ }
    }
    save();
    setInterval(save, 30000);
    document.addEventListener('visibilitychange', () => { if(!document.hidden) save(); });
  })();
  // Fetch available models from server and populate dropdown dynamically
  await populateModelDropdown();
  // Restore last-used model preference
  const savedModel=localStorage.getItem('hermes-webui-model');
  if(savedModel && $('modelSelect')){
    $('modelSelect').value=savedModel;
    // If the value didn't take (model not in list), clear the bad pref
    if($('modelSelect').value!==savedModel) localStorage.removeItem('hermes-webui-model');
  }
  // Pre-load workspace list so sidebar name is correct from first render
  await loadWorkspaceList();
  await loadOnboardingWizard();
  _initResizePanels();
  // Restore workspace panel open/closed state from last visit
  if(localStorage.getItem('hermes-webui-workspace-panel')==='open'){
    _workspacePanelMode='browse';
  }
  const saved=localStorage.getItem('hermes-webui-session');
  if(saved){
    try{await loadSession(saved);syncWorkspacePanelState();await renderSessionList();if(typeof startGatewaySSE==='function')startGatewaySSE();await checkInflightOnBoot(saved);return;}
    catch(e){localStorage.removeItem('hermes-webui-session');}
  }
  // no saved session - show empty state, wait for user to hit +
  syncTopbar();
  syncWorkspacePanelState();
  $('emptyState').style.display='';
  await renderSessionList();
  // Start real-time gateway session sync if setting is enabled
  if(typeof startGatewaySSE==='function') startGatewaySSE();
})();
