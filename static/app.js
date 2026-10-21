(function(){
  const csrf=window.INQUISITOR_CSRF;
  if(csrf){
    document.querySelectorAll('form').forEach(form=>{
      const method=(form.getAttribute('method')||'get').toLowerCase();
      if(method==='post' && !form.querySelector('input[name="csrf_token"]')){
        const input=document.createElement('input'); input.type='hidden'; input.name='csrf_token'; input.value=csrf; form.appendChild(input);
      }
    });
  }
  const getClocks=()=>[...document.querySelectorAll('.js-countdown')];
  const phase=document.getElementById('contest-phase');

  function format(ms){
    const sec=Math.floor(Math.max(0,ms)/1000);
    const h=Math.floor(sec/3600);
    const m=Math.floor((sec%3600)/60);
    const s=sec%60;
    return [h,m,s].map(v=>String(v).padStart(2,'0')).join(':');
  }

  function tick(){
    let primarySeconds=null;
    getClocks().forEach((el,index)=>{
      const raw=el.dataset.end;
      if(!raw){ el.textContent='--:--:--'; return; }
      const end=new Date(raw);
      const ms=Math.max(0,end-Date.now());
      el.textContent=format(ms);
      if(index===0) primarySeconds=Math.floor(ms/1000);
    });
    if(primarySeconds===0 && phase && phase.textContent!=='ENDED'){
      phase.textContent='ENDED';
      phase.classList.remove('running','frozen','upcoming');
      phase.classList.add('ended');
    }
  }

  if(getClocks().length){
    tick();
    setInterval(tick,1000);
  }

  const fileInput=document.getElementById('source-file');
  const languageSelect=document.getElementById('submission-language');
  const detectionNote=document.getElementById('language-detection-note');
  if(fileInput && languageSelect){
    const extensionMap={
      '.cpp':'cpp','.cc':'cpp','.cxx':'cpp',
      '.c':'c','.java':'java','.kt':'kotlin',
      '.py':'python3','.rs':'rust','.rb':'ruby'
    };
    fileInput.addEventListener('change',()=>{
      const file=fileInput.files && fileInput.files[0];
      if(!file) return;
      const name=file.name.toLowerCase();
      const dot=name.lastIndexOf('.');
      const ext=dot>=0?name.slice(dot):'';
      const detected=extensionMap[ext];
      if(detected && languageSelect.querySelector(`option[value="${detected}"]`)){
        languageSelect.value=detected;
        const label=languageSelect.options[languageSelect.selectedIndex].text;
        if(detectionNote) detectionNote.textContent=`Detected ${label} from ${ext}.`;
      }else if(detectionNote){
        detectionNote.textContent='File type not recognized; the selected language will be used.';
      }
    });
  }

  const clarificationType=document.getElementById('clarification-type');
  const clarificationProblem=document.getElementById('clarification-problem');
  if(clarificationType && clarificationProblem){
    const syncClarification=()=>{
      const needsProblem=['AMBIGUITY','ERROR'].includes(clarificationType.value);
      clarificationProblem.required=needsProblem;
    };
    clarificationType.addEventListener('change',syncClarification);
    syncClarification();
  }
})();


  const projectorScroll=document.querySelector('[data-projector-autoscroll="1"]');
  if(projectorScroll){
    let direction=1;
    let pause=false;
    setInterval(()=>{
      if(pause) return;
      const max=projectorScroll.scrollHeight-projectorScroll.clientHeight;
      if(max<=0) return;
      const next=projectorScroll.scrollTop + (direction*1.2);
      projectorScroll.scrollTop=next;
      if(projectorScroll.scrollTop>=max-2){
        direction=-1;
        pause=true;
        setTimeout(()=>{pause=false;},1200);
      }else if(projectorScroll.scrollTop<=2){
        direction=1;
        pause=true;
        setTimeout(()=>{pause=false;},900);
      }
    },20);
  }


/* Theme toggle: remember the choice in a cookie (so the server sends the right stylesheet on the first paint) and reload. */
(function () {
  var button = document.getElementById('theme-toggle');
  if (!button) return;
  button.addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    document.cookie = 'theme=' + next + '; path=/; max-age=31536000; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : '');
    location.reload();
  });
})();


/* Side menu: collapse / expand, remembered in this browser. */
(function () {
  var button = document.getElementById('sidebar-toggle');
  if (!button) return;
  var root = document.documentElement;
  function sync() { button.setAttribute('aria-expanded', root.classList.contains('sidebar-collapsed') ? 'false' : 'true'); }
  sync();
  button.addEventListener('click', function () {
    var collapsed = root.classList.toggle('sidebar-collapsed');
    try { localStorage.setItem('inq-sidebar', collapsed ? 'collapsed' : 'open'); } catch (e) {}
    sync();
  });
})();


/* Live leaderboard and judge console: the standings / submissions refresh by themselves every 15 seconds, without reloading the page. */
(function () {
  var regions = document.querySelectorAll('[data-live-region]');
  if (!regions.length || !window.fetch || !window.DOMParser) return;
  var busy = false, failures = 0;
  function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    fetch(location.href, { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Requested-With': 'live-board' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        document.querySelectorAll('[data-live-region]').forEach(function (cur) {
          var fresh = doc.querySelector('[data-live-region="' + cur.getAttribute('data-live-region') + '"]');
          // on the judge console, do not swap the table under the mouse or while a field inside it is being used
          if (cur.hasAttribute('data-live-careful') && (cur.matches(':hover') || cur.contains(document.activeElement))) return;
          if (fresh && fresh.innerHTML !== cur.innerHTML) cur.innerHTML = fresh.innerHTML;
        });
        failures = 0;
      })
      .catch(function () { failures++; })
      .then(function () { busy = false; });
  }
  // every 15 s while the tab is open; slower after repeated failures so a struggling server is not hammered
  (function loop() { setTimeout(function () { refresh(); loop(); }, failures > 2 ? 45000 : 15000); })();
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();
