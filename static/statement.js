/* Problem statements: a small, safe Markdown subset plus LaTeX math.
 *
 * Supported: **bold**, *italic*, `inline code`, ``` code blocks ```, "- " / "* " bullet lists, "1. " numbered lists,
 * blank lines between paragraphs, single line breaks kept, ![description](picture.png) for pictures uploaded to the problem,
 * and math ($x^2$, $$...$$, \( \), \[ \]; \$ is a literal dollar sign).
 *
 * The statement text is never inserted as HTML: every piece is created with createTextNode / createElement, and a picture
 * can only point at one of the problem's own uploaded files. So nothing typed in a statement can run as a script.
 */
(function () {
  'use strict';
  var ESC = '', MATH = '', CODE = '', BLOCK = '';
  var KATEX = {
    delimiters: [{left: '$$', right: '$$', display: true}, {left: '\\[', right: '\\]', display: true},
                 {left: '\\(', right: '\\)', display: false}, {left: '$', right: '$', display: false}],
    throwOnError: false, errorColor: '#ef6b70', maxExpand: 200, ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  };

  function walkText(el, fn) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), node;
    while ((node = walker.nextNode())) {
      if (!(node.parentNode.closest && node.parentNode.closest('pre, code, textarea'))) fn(node);
    }
  }
  function swap(el, from, to) {
    walkText(el, function (n) { if (n.nodeValue.indexOf(from) >= 0) n.nodeValue = n.nodeValue.split(from).join(to); });
  }
  function renderMath(el) { if (window.renderMathInElement) window.renderMathInElement(el, KATEX); }

  /* Plain text that may contain math (no Markdown): used by older pages. */
  window.renderStatementMath = function (el) {
    if (!el) return;
    swap(el, '\\$', ESC); renderMath(el); swap(el, ESC, '$');
  };

  window.renderMarkdownStatement = function (el, raw) {
    if (!el) return;
    if (raw === undefined || raw === null) raw = el.textContent;
    var names = (el.dataset.imageNames || '').split(',').filter(Boolean), base = el.dataset.imageBase || '';
    var codes = [], maths = [], blocks = [];
    var text = String(raw).replace(/\r\n?/g, '\n');

    // Protect what Markdown must not touch: fenced code, inline code, escaped dollars, then math.
    text = text.replace(/^```[^\n]*\n([\s\S]*?)^```[ \t]*$/gm, function (m, body) { blocks.push(body.replace(/\n$/, '')); return '\n' + BLOCK + (blocks.length - 1) + BLOCK + '\n'; });
    text = text.replace(/`([^`\n]+)`/g, function (m, c) { codes.push(c); return CODE + (codes.length - 1) + CODE; });
    text = text.replace(/\\\$/g, ESC);
    text = text.replace(/\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$/g, function (m) { maths.push(m); return MATH + (maths.length - 1) + MATH; });

    var INLINE = /(!\[([^\]\n]*)\]\(([^)\s]+)\))|(\*\*(?=\S)([^\n]+?)\*\*)|(\*(?=\S)([^*\n]+?)\*)|((\d+))|((\d+))/g;
    function inline(parent, s) {
      var re = new RegExp(INLINE.source, 'g'), last = 0, m;
      while ((m = re.exec(s))) {
        if (m.index > last) parent.appendChild(document.createTextNode(s.slice(last, m.index)));
        if (m[1]) {
          if (names.indexOf(m[3]) >= 0) {
            var img = document.createElement('img'); img.src = base + encodeURIComponent(m[3]); img.alt = m[2]; img.loading = 'lazy'; img.className = 'statement-img';
            parent.appendChild(img);
          } else {
            var miss = document.createElement('span'); miss.className = 'md-missing'; miss.textContent = '[picture not found: ' + m[3] + ']'; parent.appendChild(miss);
          }
        } else if (m[4]) { var b = document.createElement('strong'); inline(b, m[5]); parent.appendChild(b); }
        else if (m[6]) { var i = document.createElement('em'); inline(i, m[7]); parent.appendChild(i); }
        else if (m[8]) { var c = document.createElement('code'); c.textContent = codes[+m[9]]; parent.appendChild(c); }
        else if (m[10]) { parent.appendChild(document.createTextNode(maths[+m[11]].split(ESC).join('\\$'))); }
        last = re.lastIndex;
      }
      if (last < s.length) parent.appendChild(document.createTextNode(s.slice(last)));
    }

    var frag = document.createDocumentFragment(), cur = null;
    var blockRe = new RegExp('^' + BLOCK + '(\\d+)' + BLOCK + '$');
    text.split('\n').forEach(function (line) {
      var t = line.trim();
      if (t === '') { cur = null; return; }
      var bm = t.match(blockRe);
      if (bm) {
        cur = null; var pre = document.createElement('pre'), code = document.createElement('code');
        code.textContent = blocks[+bm[1]]; pre.appendChild(code); frag.appendChild(pre); return;
      }
      var ul = line.match(/^\s*[-*+•]\s+(.*)$/), ol = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
      if (ul || ol) {
        var type = ul ? 'ul' : 'ol';
        if (!cur || cur.type !== type) {
          cur = {type: type, el: document.createElement(type)};
          if (ol && +ol[1] !== 1) cur.el.start = +ol[1];
          frag.appendChild(cur.el);
        }
        var li = document.createElement('li'); inline(li, (ul || ol)[ul ? 1 : 2]); cur.el.appendChild(li); cur.last = li; return;
      }
      if (cur && (cur.type === 'ul' || cur.type === 'ol') && /^\s{2,}\S/.test(line)) {
        cur.last.appendChild(document.createElement('br')); inline(cur.last, t); return;
      }
      if (!cur || cur.type !== 'p') { cur = {type: 'p', el: document.createElement('p')}; frag.appendChild(cur.el); }
      else cur.el.appendChild(document.createElement('br'));
      inline(cur.el, t);
    });

    el.textContent = '';
    el.appendChild(frag);
    el.classList.add('md-rendered');
    renderMath(el);
    swap(el, ESC, '$');
  };

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.md-source').forEach(function (el) { window.renderMarkdownStatement(el); });
    document.querySelectorAll('.math-source').forEach(function (el) { window.renderStatementMath(el); });
  });
})();
