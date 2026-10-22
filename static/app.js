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
  // Every clock follows the server's time, not this computer's, so everyone sees the same countdown.
  var clockOffset=0;
  (function(){
    var meta=document.querySelector('meta[name="server-now"]');
    var serverMs=meta?Date.parse(meta.content):NaN;
    if(!isNaN(serverMs)) clockOffset=serverMs-Date.now();
  })();
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
      const ms=Math.max(0,end-(Date.now()+clockOffset));
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


  // Auto-scroll is the choice of whoever is looking at this screen (remembered in this browser only).
  const projectorScroll=document.querySelector('[data-projector-autoscroll="1"]');
  if(projectorScroll){
    const scrollButton=document.getElementById('autoscroll-toggle');
    const fromAddress=new URLSearchParams(location.search).get('scroll');
    let scrolling=false;
    try{ scrolling=localStorage.getItem('inq-projector-scroll')==='on'; }catch(e){}
    if(fromAddress==='1') scrolling=true;
    if(fromAddress==='0') scrolling=false;
    const showScrollState=()=>{ if(scrollButton){ scrollButton.textContent='Auto-scroll: '+(scrolling?'on':'off'); scrollButton.setAttribute('aria-pressed',scrolling?'true':'false'); } };
    showScrollState();
    if(scrollButton) scrollButton.addEventListener('click',()=>{
      scrolling=!scrolling;
      try{ localStorage.setItem('inq-projector-scroll',scrolling?'on':'off'); }catch(e){}
      showScrollState();
    });
    let direction=1;
    let pause=false;
    setInterval(()=>{
      if(!scrolling||pause) return;
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


/* Live leaderboard: the standings refresh by themselves every 15 seconds, without reloading the page. */
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


/* Contestants page: select several contestants and register or unregister them together. */
(function () {
  var bar = document.getElementById('bulk-form');
  if (!bar) return;
  var boxes = function () { return Array.prototype.slice.call(document.querySelectorAll('input[name="ids"][form="bulk-form"]')); };
  bar.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-select]');
    if (!b) return;
    var mode = b.getAttribute('data-select');
    boxes().forEach(function (x) { x.checked = mode === 'all' || (mode === 'registered' && x.getAttribute('data-registered') === '1'); });
  });
  bar.addEventListener('submit', function (e) {
    var s = e.submitter, msg = s && s.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();


/* Notices: a new announcement reaches the people in the contest while they work. A toast slides in (it stays until it is closed), the
   Announcements link in the side menu shows how many are unread, and the "Contest notice" banner appears and disappears by itself. */
(function () {
  var body = document.body, url = body.getAttribute('data-notices'), annUrl = body.getAttribute('data-announcements-url');
  if (!url || !window.fetch) return;
  function load(k, fallback) { try { var v = JSON.parse(localStorage.getItem(k) || 'null'); return v === null ? fallback : v; } catch (e) { return fallback; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  var KEY_READ = 'inq-ann-read', KEY_DISMISSED = 'inq-ann-dismissed';
  var read = load(KEY_READ, 0), dismissed = load(KEY_DISMISSED, []);
  var page = document.querySelector('[data-announcements-max]');
  if (page) { read = Math.max(read, parseInt(page.getAttribute('data-announcements-max'), 10) || 0); save(KEY_READ, read); }

  var host = document.createElement('div');
  host.className = 'notice-toasts'; host.setAttribute('role', 'status'); host.setAttribute('aria-live', 'polite');
  document.body.appendChild(host);

  function setBadge(n) {
    var link = annUrl ? document.querySelector('a[href="' + annUrl + '"]') : null;
    if (!link) return;
    var b = link.querySelector('.nav-badge');
    if (!n) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement('span'); b.className = 'nav-badge'; link.appendChild(b); }
    b.textContent = n > 9 ? '9+' : String(n);
  }
  function setBanner(text) {
    var banner = document.querySelector('.emergency-banner');
    if (!text) { if (banner) banner.remove(); return; }
    if (!banner) {
      banner = document.createElement('div'); banner.className = 'emergency-banner';
      banner.appendChild(document.createElement('strong')).textContent = 'Contest notice';
      banner.appendChild(document.createElement('span'));
      var anchor = document.querySelector('.topbar');
      if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(banner, anchor.nextSibling); else document.body.insertBefore(banner, document.body.firstChild);
    }
    var span = banner.querySelector('span'); if (span && span.textContent !== text) span.textContent = text;
  }
  function closeToast(el, id) {
    if (id !== null) { dismissed.push(id); dismissed = dismissed.slice(-50); save(KEY_DISMISSED, dismissed); }
    el.classList.add('leaving'); setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 200);
  }
  function showToast(item) {
    if (host.querySelector('[data-id="' + item.id + '"]')) return;
    var el = document.createElement('div'); el.className = 'notice-toast ' + (item.severity || 'info'); el.setAttribute('data-id', item.id);
    var head = document.createElement('div'); head.className = 'notice-toast-head';
    var label = document.createElement('span'); label.className = 'notice-toast-label'; label.textContent = 'New announcement'; head.appendChild(label);
    var x = document.createElement('button'); x.type = 'button'; x.className = 'notice-toast-x'; x.setAttribute('aria-label', 'Dismiss'); x.textContent = '\u00d7'; head.appendChild(x);
    var title = document.createElement('strong'); title.textContent = item.title;
    var text = document.createElement('p'); text.textContent = item.body.length > 220 ? item.body.slice(0, 217) + '...' : item.body;
    var go = document.createElement('a'); go.className = 'notice-toast-go'; go.href = annUrl || '#'; go.textContent = 'Read announcement';
    el.appendChild(head); el.appendChild(title); el.appendChild(text); el.appendChild(go);
    x.addEventListener('click', function () { closeToast(el, item.id); });
    host.appendChild(el);
    while (host.children.length > 3) host.removeChild(host.firstChild);
  }
  var busy = false;
  function poll() {
    if (busy || document.hidden) return;
    busy = true;
    fetch(url + '?since=' + read, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        setBadge(d.unread); setBanner(d.emergency || '');
        var now = Date.now();
        d.items.forEach(function (it) {
          if (it.id <= read || dismissed.indexOf(it.id) >= 0) return;
          var age = now - Date.parse(it.at);
          if (age <= 15 * 60 * 1000 || it.pinned || it.severity !== 'info') showToast(it);   // an old, ordinary one only counts in the badge
        });
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  poll();
  setInterval(poll, 15000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
