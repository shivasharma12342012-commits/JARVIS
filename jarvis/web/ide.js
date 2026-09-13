/* =============================================================================
   The Code surface

   A second window inside the same window. It shares the title bar, the theme
   and the event stream with the conversation and nothing else: its own layout,
   its own keyboard map, its own idea of what the screen is for.

   The editor is a transparent textarea sitting exactly on top of a highlighted
   copy of the same text. That arrangement is chosen for one reason above all
   others: every edit goes through the browser's own editing machinery, so undo,
   redo, autorepeat, IME composition, spell-check suppression, accessibility and
   the caret all behave the way the platform says they should, for free. The
   cost is that the two layers must agree to the pixel — which is why every edit
   below is made with `execCommand('insertText')` rather than by assigning to
   `value`, and why the font rules for both layers are written as a single block
   in the stylesheet.
   ============================================================================= */
(function () {
'use strict';

const L = window.Lang;
const INDENT = '    ';
const MAX_HISTORY = 40;

/* Re-highlighting a four-thousand-line file on every keystroke is out of the
   question, so the editor keeps a line-per-line model and repaints only what
   actually changed — plus however far a newly opened comment or docstring
   carries past it. */
const Doc = {
  make(data) {
    return {
      path: data.path, name: data.name,
      language: data.language || 'text',
      text: data.content || '',
      saved: data.content || '',
      digest: data.digest || '',
      editable: data.editable !== false,
      truncated: !!data.truncated,
      size: data.size || 0,
      scroll: 0, caret: 0,
      eol: /\r\n/.test(data.content || '') ? 'CRLF' : 'LF',
    };
  },
  dirty(doc) { return doc && doc.text !== doc.saved; },
};

/* ── the editor ─────────────────────────────────────────────────────────── */
const Ed = {
  doc: null,
  lines: [],
  opens: [],          // the block open on entry to each line, as a key
  nodes: [],          // one highlight element per line
  gutter: [],         // one number element per line
  wrapped: false,
  map: true,

  init() {
    this.input = $('ed-input');
    this.lin = $('lin');
    this.gin = $('gin');
    this.host = $('ed');
    this.canvas = $('ed-map');

    this.input.addEventListener('input', () => this.onInput());
    this.input.addEventListener('scroll', () => this.onScroll());
    this.input.addEventListener('keydown', (event) => this.onKey(event));
    this.input.addEventListener('click', () => this.onCaret());
    this.input.addEventListener('select', () => this.onCaret());
    this.input.addEventListener('keyup', () => this.onCaret());
    this.input.addEventListener('blur', () => this.stash());

    this.canvas.addEventListener('mousedown', (event) => this.mapJump(event));
    this.canvas.addEventListener('mousemove', (event) => {
      if (event.buttons === 1) this.mapJump(event);
    });

    try {
      this.wrapped = localStorage.getItem('jarvis.ide.wrap') === '1';
      this.map = localStorage.getItem('jarvis.ide.map') !== '0';
    } catch (err) { /* private mode */ }
    this.applyWrap();
    this.host.classList.toggle('no-map', !this.map);
  },

  /* -- loading and unloading ---------------------------------------------- */
  load(doc) {
    this.stash();
    this.doc = doc;
    $('ed-empty').hidden = true;
    this.host.hidden = false;
    this.input.value = doc.text;
    this.input.readOnly = !doc.editable;
    this.rebuild();
    this.input.scrollTop = doc.scroll || 0;
    this.input.setSelectionRange(doc.caret || 0, doc.caret || 0);
    this.onScroll();
    this.onCaret();
    Ide.afterLoad(doc);
  },

  blank() {
    this.stash();
    this.doc = null;
    this.input.value = '';
    this.lines = []; this.opens = []; this.nodes = []; this.gutter = [];
    this.lin.textContent = '';
    this.gin.textContent = '';
    this.host.hidden = true;
    $('ed-empty').hidden = false;
    Ide.afterLoad(null);
  },

  /* The caret and the scroll position are a tab's, not the editor's: switching
     away and back should put you exactly where you were. */
  stash() {
    if (!this.doc) return;
    this.doc.scroll = this.input.scrollTop;
    this.doc.caret = this.input.selectionStart;
    this.doc.text = this.input.value;
  },

  /* -- painting ------------------------------------------------------------ */
  rebuild() {
    const text = this.input.value;
    this.lines = text.split('\n');
    this.opens = new Array(this.lines.length + 1);
    this.nodes = [];
    this.gutter = [];

    const body = document.createDocumentFragment();
    const rail = document.createDocumentFragment();
    const state = {};
    for (let i = 0; i < this.lines.length; i++) {
      this.opens[i] = key(state.open);
      body.appendChild(this.paint(i, state));
      rail.appendChild(this.number(i));
    }
    this.opens[this.lines.length] = key(state.open);

    this.lin.textContent = '';
    this.gin.textContent = '';
    this.lin.appendChild(body);
    this.gin.appendChild(rail);
    this.drawMap();
  },

  paint(i, state) {
    const node = document.createElement('div');
    node.className = 'eline';
    node.innerHTML = L.highlight(this.lines[i], this.doc ? this.doc.language : 'text', state) || '&nbsp;';
    this.nodes[i] = node;
    return node;
  },

  number(i) {
    const node = document.createElement('div');
    node.className = 'gln';
    node.textContent = String(i + 1);
    this.gutter[i] = node;
    return node;
  },

  /* Repaint the smallest span that can possibly have changed.

     The interesting half is the tail: opening a docstring on line 40 re-colours
     every line after it, so after the edited span the repaint keeps walking
     down while the block state it computes differs from the one recorded on the
     previous pass, and stops the moment the two agree again. */
  repaint() {
    const next = this.input.value.split('\n');
    const prev = this.lines;

    let head = 0;
    const shortest = Math.min(prev.length, next.length);
    while (head < shortest && prev[head] === next[head]) head += 1;
    let tail = 0;
    while (tail < shortest - head &&
           prev[prev.length - 1 - tail] === next[next.length - 1 - tail]) tail += 1;

    const removed = prev.length - tail - head;
    const added = next.length - tail - head;

    // Swap the changed span's nodes out.
    for (let i = 0; i < removed; i++) {
      this.nodes[head + i].remove();
      this.gutter[prev.length - 1 - i].remove();
    }
    this.nodes.splice(head, removed);
    this.gutter.splice(prev.length - removed, removed);
    this.lines = next;
    this.opens.length = next.length + 1;

    const state = { open: unkey(this.opens[head]) };
    const before = this.nodes[head] || null;
    const fresh = document.createDocumentFragment();
    const holder = [];
    for (let i = head; i < head + added; i++) {
      this.opens[i] = key(state.open);
      const node = document.createElement('div');
      node.className = 'eline';
      node.innerHTML = L.highlight(next[i], this.doc ? this.doc.language : 'text', state) || '&nbsp;';
      holder.push(node);
      fresh.appendChild(node);
    }
    this.lin.insertBefore(fresh, before);
    this.nodes.splice(head, 0, ...holder);

    // Line numbers are positional, so only the count matters.
    for (let i = this.gutter.length; i < next.length; i++) this.gin.appendChild(this.number(i));

    // Carry on down until the block state settles.
    let i = head + added;
    while (i < next.length) {
      const was = this.opens[i];
      const now = key(state.open);
      if (was === now) break;
      this.opens[i] = now;
      const node = this.nodes[i];
      if (!node) break;
      node.innerHTML = L.highlight(next[i], this.doc ? this.doc.language : 'text', state) || '&nbsp;';
      i += 1;
    }
    this.opens[next.length] = key(state.open);
  },

  /* -- events -------------------------------------------------------------- */
  onInput() {
    if (!this.doc) return;
    this.repaint();
    this.doc.text = this.input.value;
    this.onCaret();
    this.flare();
    Ide.touched();
    this.schedule();
  },

  /* A short flare on the line being typed into. Purely decorative and gone in
     a third of a second, which is the whole argument for it being allowed. */
  flare() {
    if (REDUCED) return;
    const node = this.nodes[this.lineAt(this.input.selectionStart)];
    if (!node) return;
    node.classList.remove('hit');
    void node.offsetWidth;      // restart the animation
    node.classList.add('hit');
  },

  /* Linting and the minimap are expensive and never urgent. */
  schedule() {
    clearTimeout(this._soon);
    this._soon = setTimeout(() => {
      this.drawMap();
      this.markChanges();
      Ide.problems.run();
      Ide.outline.run();
    }, 220);
  },

  /* Which lines differ from what is on disk. A line-for-line comparison after a
     common prefix and suffix, which is the same shape the repaint uses and is
     right for the thing it is describing: what you have touched in this sitting.
     It is not a diff algorithm and does not pretend to be one. */
  markChanges() {
    if (!this.doc) return;
    const saved = this.doc.saved.split('\n');
    const now = this.lines;
    this.gutter.forEach((node) => node.classList.remove('added', 'changed'));
    if (this.doc.saved === this.doc.text) return;

    let head = 0;
    const shortest = Math.min(saved.length, now.length);
    while (head < shortest && saved[head] === now[head]) head += 1;
    let tail = 0;
    while (tail < shortest - head &&
           saved[saved.length - 1 - tail] === now[now.length - 1 - tail]) tail += 1;

    const grew = now.length > saved.length;
    for (let i = head; i < now.length - tail; i++) {
      if (!this.gutter[i]) continue;
      this.gutter[i].classList.add(grew && i >= head + (saved.length - tail - head) ? 'added' : 'changed');
    }
  },

  onScroll() {
    const x = this.input.scrollLeft;
    const y = this.input.scrollTop;
    this.lin.style.transform = 'translate(' + (-x) + 'px,' + (-y) + 'px)';
    this.gin.style.transform = 'translateY(' + (-y) + 'px)';
    this.drawViewport();
  },

  onCaret() {
    if (!this.doc) return;
    const at = this.input.selectionStart;
    const line = this.lineAt(at);
    const column = at - this.startOf(line);

    if (this._cur !== line) {
      if (this.nodes[this._cur]) this.nodes[this._cur].classList.remove('cur');
      if (this.gutter[this._cur]) this.gutter[this._cur].classList.remove('on');
      if (this.nodes[line]) this.nodes[line].classList.add('cur');
      if (this.gutter[line]) this.gutter[line].classList.add('on');
      this._cur = line;
    }

    const selected = this.input.selectionEnd - this.input.selectionStart;
    $('st-position').textContent =
      'Ln ' + (line + 1) + ', Col ' + (column + 1) + (selected ? '  (' + selected + ')' : '');
    Ide.mission.scope(selected > 0);
  },

  lineAt(index) {
    let line = 0;
    let seen = 0;
    for (let i = 0; i < this.lines.length; i++) {
      const end = seen + this.lines[i].length;
      if (index <= end) { line = i; break; }
      seen = end + 1;
      line = i + 1;
    }
    return Math.min(line, Math.max(0, this.lines.length - 1));
  },

  startOf(line) {
    let at = 0;
    for (let i = 0; i < line && i < this.lines.length; i++) at += this.lines[i].length + 1;
    return at;
  },

  goto(line, column) {
    if (!this.doc) return;
    const target = clamp(line - 1, 0, Math.max(0, this.lines.length - 1));
    const at = this.startOf(target) + (column || 0);
    this.input.focus({ preventScroll: true });
    this.input.setSelectionRange(at, at);
    // Put the line a third of the way down rather than at the very top: you
    // almost always want to see what comes before what you jumped to.
    const height = this.input.clientHeight;
    const lineHeight = this.lineHeight();
    this.input.scrollTop = Math.max(0, target * lineHeight - height / 3);
    this.onScroll();
    this.onCaret();
  },

  lineHeight() {
    return parseFloat(getComputedStyle(this.host).getPropertyValue('--ed-line-h')) || 21;
  },

  /* -- writing ------------------------------------------------------------- */
  /* Everything that changes the text goes through here. `insertText` keeps the
     browser's own undo stack intact, which is the entire reason the editor is
     built on a textarea rather than a contenteditable div. */
  put(text, from, to) {
    this.input.focus({ preventScroll: true });
    if (from !== undefined) this.input.setSelectionRange(from, to === undefined ? from : to);
    const ok = document.execCommand && document.execCommand('insertText', false, text);
    if (!ok) {
      // Some engines refuse execCommand. Falling back loses undo, which is worse
      // than nothing but much better than an editor that will not type.
      const start = this.input.selectionStart;
      const end = this.input.selectionEnd;
      const value = this.input.value;
      this.input.value = value.slice(0, start) + text + value.slice(end);
      this.input.setSelectionRange(start + text.length, start + text.length);
    }
    this.onInput();
  },

  select(from, to) {
    this.input.focus({ preventScroll: true });
    this.input.setSelectionRange(from, to === undefined ? from : to);
    this.onCaret();
  },

  /* A key the editor acts on is a key nothing else should also act on. Stopping
     propagation as well as the default is not belt and braces: the conversation
     registers its own map on `document`, and a shortcut that both layers claim
     fires both. Ctrl+Shift+K deleted a line *and* opened the command palette on
     top of it until this was here. */
  take(event) {
    event.preventDefault();
    event.stopPropagation();
    return true;
  },

  onKey(event) {
    const meta = event.ctrlKey || event.metaKey;
    const key = event.key;
    if (!this.doc) return;

    // The editor owns Escape while the cursor is in it.
    if (key === 'Escape') {
      event.stopPropagation();
      if (!$('findbar').hidden) Ide.find.hide();
      return;
    }

    if (meta && !event.altKey) {
      const lower = key.toLowerCase();
      // Copy, cut, paste, select-all, undo and redo belong to the platform.
      if (!event.shiftKey && 'azycvx'.indexOf(lower) !== -1) return;
      if (event.shiftKey && lower === 'z') return;                  // redo, the other spelling
      if (!event.shiftKey && lower === 's') return this.take(event) && Ide.save();
      if (!event.shiftKey && lower === 'f') return this.take(event) && Ide.find.show(false);
      if (!event.shiftKey && lower === 'h') return this.take(event) && Ide.find.show(true);
      if (!event.shiftKey && lower === 'g') return this.take(event) && Ide.gotoLine();
      if (!event.shiftKey && lower === 'd') return this.take(event) && this.duplicate();
      if (key === '/') return this.take(event) && this.comment();
      if (event.shiftKey && lower === 'k') return this.take(event) && this.deleteLines();
    }
    if (event.altKey && (key === 'ArrowUp' || key === 'ArrowDown')) {
      this.take(event); this.moveLines(key === 'ArrowUp' ? -1 : 1); return;
    }
    if (event.altKey && key.toLowerCase() === 'z') { this.take(event); Ide.toggleWrap(); return; }

    if (this.input.readOnly) return;

    if (key === 'Tab') { this.take(event); this.tab(event.shiftKey); return; }
    if (key === 'Enter') { this.take(event); this.enter(); return; }
    if (key === 'Backspace') { if (this.backspace()) this.take(event); return; }
    if (key === 'Home' && !event.shiftKey && !meta) { if (this.home()) this.take(event); return; }
    if (key.length === 1 && !meta && !event.altKey && this.pair(key)) { this.take(event); }
  },

  /* -- the editing operations --------------------------------------------- */
  indentOf(line) {
    const match = (this.lines[line] || '').match(/^[ \t]*/);
    return match ? match[0] : '';
  },

  enter() {
    const at = this.input.selectionStart;
    const line = this.lineAt(at);
    const indent = this.indentOf(line);
    const before = this.input.value.slice(this.startOf(line), at);
    const after = this.input.value.slice(this.input.selectionEnd);
    const lang = this.doc.language;

    let next = indent;
    if (L.opensBlock(lang, before)) next += INDENT;

    // Caret sitting between a bracket and its partner: open the pair out into
    // three lines with the caret on the empty middle one.
    const opener = before.slice(-1);
    const closer = after.slice(0, 1);
    if (L.PAIRS[opener] === closer && closer) {
      this.put('\n' + next + '\n' + indent);
      const to = at + 1 + next.length;
      this.select(to, to);
      return;
    }
    this.put('\n' + next);
  },

  tab(back) {
    const start = this.input.selectionStart;
    const end = this.input.selectionEnd;
    const first = this.lineAt(start);
    const last = this.lineAt(end);

    if (!back && first === last && start === end) { this.put(INDENT); return; }

    const from = this.startOf(first);
    const to = this.startOf(last) + this.lines[last].length;
    const block = this.input.value.slice(from, to).split('\n').map((line) => {
      if (back) return line.replace(/^(?: {1,4}|\t)/, '');
      return line.length || first === last ? INDENT + line : line;
    }).join('\n');

    this.put(block, from, to);
    this.select(from, from + block.length);
  },

  /* A Backspace in leading whitespace removes a whole indent, and a Backspace
     between a bracket and its partner removes both. Everywhere else it is the
     platform's Backspace, untouched. */
  backspace() {
    const at = this.input.selectionStart;
    if (at !== this.input.selectionEnd || at === 0) return false;
    const value = this.input.value;

    if (L.PAIRS[value[at - 1]] === value[at]) {
      this.put('', at - 1, at + 1);
      return true;
    }
    const line = this.lineAt(at);
    const head = value.slice(this.startOf(line), at);
    if (head.length && /^ +$/.test(head)) {
      const back = ((head.length - 1) % INDENT.length) + 1;
      this.put('', at - back, at);
      return true;
    }
    return false;
  },

  home() {
    const at = this.input.selectionStart;
    if (at !== this.input.selectionEnd) return false;
    const line = this.lineAt(at);
    const start = this.startOf(line);
    const indent = this.indentOf(line).length;
    const target = at === start + indent ? start : start + indent;
    this.select(target, target);
    return true;
  },

  /* Typing an opening bracket closes it; typing a closing one when it is
     already there steps over it; typing either around a selection wraps it. */
  pair(char) {
    const start = this.input.selectionStart;
    const end = this.input.selectionEnd;
    const value = this.input.value;
    const closer = L.PAIRS[char];

    if (start !== end && (closer || L.CLOSERS[char])) {
      const open = closer ? char : L.CLOSERS[char];
      const shut = closer || char;
      const inner = value.slice(start, end);
      this.put(open + inner + shut, start, end);
      this.select(start + 1, start + 1 + inner.length);
      return true;
    }
    if (!closer) return false;
    if (value[start] === char && (char === '"' || char === "'" || char === '`')) {
      this.select(start + 1, start + 1);
      return true;
    }
    // Not inside a word: `don't` must not become `don''t`.
    const before = value[start - 1] || '';
    const after = value[start] || '';
    if ((char === '"' || char === "'" || char === '`') && /[\w'"`]/.test(before)) return false;
    if (/[\w$]/.test(after)) return false;

    this.put(char + closer);
    this.select(start + 1, start + 1);
    return true;
  },

  comment() {
    const token = L.commentToken(this.doc.language);
    const block = L.blockComment(this.doc.language);
    const first = this.lineAt(this.input.selectionStart);
    const last = this.lineAt(this.input.selectionEnd);
    const from = this.startOf(first);
    const to = this.startOf(last) + this.lines[last].length;
    const span = this.input.value.slice(from, to);

    if (block) {
      const trimmed = span.trim();
      const wrapped = trimmed.startsWith(block[0]) && trimmed.endsWith(block[1]);
      const next = wrapped
        ? span.replace(block[0], '').replace(new RegExp(escapeRe(block[1]) + '\\s*$'), '').trim()
        : block[0] + ' ' + span + ' ' + block[1];
      this.put(next, from, to);
      this.select(from, from + next.length);
      return;
    }
    if (!token) { toast('That language has no comments this editor knows about.'); return; }

    const rows = span.split('\n');
    const marked = rows.every((row) => !row.trim() || row.trimStart().startsWith(token));
    const next = rows.map((row) => {
      if (!row.trim()) return row;
      if (marked) return row.replace(new RegExp('^(\\s*)' + escapeRe(token) + ' ?'), '$1');
      const indent = row.match(/^\s*/)[0];
      return indent + token + ' ' + row.slice(indent.length);
    }).join('\n');

    this.put(next, from, to);
    this.select(from, from + next.length);
  },

  duplicate() {
    const start = this.input.selectionStart;
    const end = this.input.selectionEnd;
    if (start !== end) {
      const text = this.input.value.slice(start, end);
      this.put(text, end, end);
      this.select(end, end + text.length);
      return;
    }
    const line = this.lineAt(start);
    const from = this.startOf(line);
    const text = this.lines[line];
    this.put('\n' + text, from + text.length, from + text.length);
    const at = start + text.length + 1;
    this.select(at, at);
  },

  deleteLines() {
    const first = this.lineAt(this.input.selectionStart);
    const last = this.lineAt(this.input.selectionEnd);
    const from = this.startOf(first);
    const to = Math.min(this.input.value.length, this.startOf(last) + this.lines[last].length + 1);
    this.put('', from, to);
    this.select(Math.min(from, this.input.value.length));
  },

  moveLines(step) {
    const first = this.lineAt(this.input.selectionStart);
    const last = this.lineAt(this.input.selectionEnd);
    if (step < 0 && first === 0) return;
    if (step > 0 && last >= this.lines.length - 1) return;

    const offset = this.input.selectionStart - this.startOf(first);
    const length = this.input.selectionEnd - this.input.selectionStart;
    const block = this.lines.slice(first, last + 1);
    const other = step < 0 ? this.lines[first - 1] : this.lines[last + 1];
    const rows = step < 0 ? block.concat([other]) : [other].concat(block);

    const from = this.startOf(step < 0 ? first - 1 : first);
    const lastRow = step < 0 ? last : last + 1;
    const to = this.startOf(lastRow) + this.lines[lastRow].length;

    const joined = rows.join('\n');
    this.put(joined, from, to);
    const moved = this.startOf(first + step) + offset;
    this.select(moved, moved + length);
  },

  /* -- wrap and minimap ---------------------------------------------------- */
  applyWrap() {
    this.host.classList.toggle('wrapped', this.wrapped);
    this.input.wrap = this.wrapped ? 'soft' : 'off';
    $('st-wrap').textContent = this.wrapped ? 'Wrap' : 'No wrap';
    // Wrapping makes a logical line taller than one row, so the gutter can no
    // longer assume a fixed height per number and has to be measured against
    // the lines it labels.
    if (this.wrapped) requestAnimationFrame(() => this.measureGutter());
    else this.gutter.forEach((node) => { node.style.height = ''; });
  },

  measureGutter() {
    for (let i = 0; i < this.gutter.length; i++) {
      const line = this.nodes[i];
      if (!line || !this.gutter[i]) continue;
      this.gutter[i].style.height = line.offsetHeight + 'px';
    }
  },

  /* The minimap is drawn, not laid out: one short bar per line, its width from
     the line's length and its colour from how much of it is comment. Cheap
     enough to redraw whole, which is the only reason it can stay honest about a
     file that is being edited. */
  drawMap() {
    if (!this.map || !this.doc) return;
    const canvas = this.canvas;
    const box = canvas.getBoundingClientRect();
    if (!box.height) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(box.width * dpr));
    canvas.height = Math.max(1, Math.round(box.height * dpr));
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, box.width, box.height);

    const count = this.lines.length || 1;
    const step = Math.max(0.6, Math.min(3, box.height / count));
    const styles = getComputedStyle(this.host);
    const text = styles.getPropertyValue('--text-dim').trim() || '#888';
    const faint = styles.getPropertyValue('--text-faint').trim() || '#555';
    const accent = styles.getPropertyValue('--accent').trim() || '#888';

    const shown = Math.min(count, Math.floor(box.height / Math.max(0.6, step)) || count);
    const stride = Math.max(1, Math.ceil(count / shown));
    for (let i = 0; i < count; i += stride) {
      const line = this.lines[i];
      if (!line || !line.trim()) continue;
      const indent = line.length - line.trimStart().length;
      const comment = this.opens[i] || /^\s*(#|\/\/|--|;|%)/.test(line);
      ctx.fillStyle = comment ? faint : (/^\s*(def |class |function |fn |func |pub )/.test(line) ? accent : text);
      ctx.globalAlpha = comment ? .3 : .55;
      const y = (i / count) * box.height;
      const x = 3 + Math.min(26, indent * 1.6);
      const width = Math.min(box.width - x - 6, line.trim().length * 0.46);
      ctx.fillRect(x, y, Math.max(1, width), Math.max(1, step * 0.7));
    }
    ctx.globalAlpha = 1;
    this.drawViewport();
  },

  drawViewport() {
    if (!this.map || !this.doc) return;
    const box = this.canvas.getBoundingClientRect();
    if (!box.height) return;
    const total = this.input.scrollHeight || 1;
    const top = (this.input.scrollTop / total) * box.height;
    const height = Math.max(14, (this.input.clientHeight / total) * box.height);
    let band = this._band;
    if (!band) {
      band = this._band = el('div', 'ed-map-band');
      this.canvas.parentNode.appendChild(band);
    }
    band.style.top = Math.round(top) + 'px';
    band.style.height = Math.round(height) + 'px';
    band.hidden = this.canvas.offsetParent === null;
  },

  mapJump(event) {
    const box = this.canvas.getBoundingClientRect();
    const share = clamp((event.clientY - box.top) / box.height, 0, 1);
    this.input.scrollTop = share * this.input.scrollHeight - this.input.clientHeight / 2;
    this.onScroll();
  },
};

const key = (open) => (open ? open.close + '\u0000' + open.cls : '');
const unkey = (text) => {
  if (!text) return null;
  const parts = text.split('\u0000');
  return { close: parts[0], cls: parts[1] };
};
const escapeRe = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/* ── the surface ────────────────────────────────────────────────────────── */
const Ide = {
  docs: new Map(),
  order: [],
  current: null,
  index: null,
  artifacts: new Set(),

  init() {
    if (!L) return;                       // lang.js failed to load; stay out of the way
    Ed.init();
    this.tree.init();
    this.find.init();
    this.quick.init();
    this.search.init();
    this.outline.init();
    this.problems.init();
    this.dock.init();
    this.mission.init();
    this.panels.init();
    this.seams.init();
    this.glass();
    this.observe();
    this.keys();

    $('st-wrap').onclick = () => this.toggleWrap();
    $('st-position').onclick = () => this.gotoLine();
    $('st-language').onclick = () => this.pickLanguage();
    $('st-problems').onclick = () => { this.panels.show('problems'); this.dock.show('problems'); };
    $('st-branch-name').textContent = BOOT.workspaceName || 'workspace';
    $('btn-ide-appearance').onclick = () => Studio.toggle();
    $('btn-ide-keys').onclick = () => Keys.show();

    document.querySelectorAll('.surface-btn').forEach((button) => {
      button.onclick = () => this.surface(button.dataset.surface);
    });
    this.inkTo(document.querySelector('.surface-btn.on'));
    window.addEventListener('resize', () => {
      this.inkTo(document.querySelector('.surface-btn.on'));
      if (this.showing) { Ed.drawMap(); if (Ed.wrapped) Ed.measureGutter(); }
    });
  },

  /* -- switching surfaces -------------------------------------------------- */
  get showing() { return document.body.dataset.surface === 'code'; },

  surface(which, options) {
    const code = which === 'code';
    document.body.dataset.surface = code ? 'code' : 'chat';
    $('ide').hidden = !code;
    $('shell').hidden = code;
    document.querySelectorAll('.surface-btn').forEach((button) => {
      const on = (button.dataset.surface === 'code') === code;
      button.classList.toggle('on', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) this.inkTo(button);
    });
    try { localStorage.setItem('jarvis.surface', code ? 'code' : 'chat'); } catch (err) { /* private */ }

    this.dock.reparent(code);
    if (!code) { Ed.stash(); if (!(options && options.quiet)) Composer.input.focus(); return; }
    if (!this.index) this.loadIndex();
    requestAnimationFrame(() => {
      Ed.drawMap();
      if (Ed.wrapped) Ed.measureGutter();
      if (Ed.doc && !(options && options.quiet)) Ed.input.focus({ preventScroll: true });
    });
  },

  inkTo(button) {
    const ink = document.querySelector('.surface-ink');
    if (!ink || !button) return;
    ink.style.width = button.offsetWidth + 'px';
    ink.style.transform = 'translateX(' + (button.offsetLeft - 2) + 'px)';
  },

  /* -- opening files ------------------------------------------------------- */
  async open(path, options) {
    const at = options && options.line;
    if (!(options && options.stay)) this.surface('code', { quiet: true });

    if (this.docs.has(path)) {
      this.select(path);
      if (at) Ed.goto(at, options.column || 0);
      return;
    }
    let data;
    try { data = await api('/api/file?path=' + encodeURIComponent(path)); }
    catch (err) { toast('That file would not open.'); return; }
    if (!data || data.error) { toast((data && data.error) || 'That file would not open.'); return; }
    if (data.binary) { toast(data.name + ' is a binary file.'); return; }

    this.docs.set(path, Doc.make(data));
    this.order.push(path);
    this.select(path);
    if (at) Ed.goto(at, options.column || 0);
    if (data.truncated) toast(data.name + ' is long; it is shown up to the read limit and cannot be saved.');
  },

  select(path) {
    const doc = this.docs.get(path);
    if (!doc) return;
    this.current = path;
    Ed.load(doc);
    this.tabs();
    this.crumbs(doc);
  },

  close(path) {
    const doc = this.docs.get(path);
    if (doc && Doc.dirty(doc) && !confirm(doc.name + ' has unsaved changes. Close it anyway?')) return;
    this.docs.delete(path);
    this.order = this.order.filter((item) => item !== path);
    if (this.current !== path) { this.tabs(); return; }
    this.current = null;
    if (this.order.length) this.select(this.order[this.order.length - 1]);
    else { Ed.blank(); this.tabs(); this.crumbs(null); }
  },

  afterLoad(doc) {
    $('st-language').textContent = doc ? L.label(doc.language) : 'Plain text';
    $('st-eol').textContent = doc ? doc.eol : 'LF';
    $('st-indent').textContent = doc ? indentStyle(doc.text) : 'Spaces: 4';
    $('st-save').textContent = '';
    $('st-save').className = '';
    this.outline.run();
    this.problems.run();
    this.mission.scope(false);
    if (!doc) $('st-position').textContent = 'Ln 1, Col 1';
  },

  tabs() {
    const strip = $('etabs');
    strip.textContent = '';
    $('artifact-count').textContent = String(this.order.length);
    this.order.forEach((path) => {
      const doc = this.docs.get(path);
      if (!doc) return;
      const tab = el('button', 'etab' + (path === this.current ? ' on' : '') +
                                (Doc.dirty(doc) ? ' dirty' : ''));
      tab.type = 'button';
      tab.title = path;
      tab.append(el('span', 'tname', doc.name));
      if (Doc.dirty(doc)) tab.append(el('span', 'tdot'));
      const shut = el('span', 'tx', '\u00d7');
      shut.onclick = (event) => { event.stopPropagation(); this.close(path); };
      tab.append(shut);
      tab.onclick = () => this.select(path);
      // Middle-click closes a tab, as it does in every editor and browser.
      tab.onauxclick = (event) => { if (event.button === 1) { event.preventDefault(); this.close(path); } };
      strip.appendChild(tab);
    });
  },

  crumbs(doc) {
    const bar = $('ebread');
    bar.textContent = '';
    if (!doc) return;
    bar.appendChild(el('span', 'crumb', BOOT.workspaceName || 'workspace'));
    String(doc.path).split('/').forEach((part, i, all) => {
      bar.appendChild(el('span', 'csep', '\u203a'));
      bar.appendChild(el('span', 'crumb' + (i === all.length - 1 ? ' leaf' : ''), part));
    });
  },

  touched() {
    const doc = this.docs.get(this.current);
    if (!doc) return;
    const tab = document.querySelector('.etab.on');
    if (tab) tab.classList.toggle('dirty', Doc.dirty(doc));
    if (tab && Doc.dirty(doc) && !tab.querySelector('.tdot')) {
      tab.insertBefore(el('span', 'tdot'), tab.querySelector('.tx'));
    }
    $('st-save').textContent = Doc.dirty(doc) ? 'unsaved' : '';
    $('st-save').className = '';
  },

  /* -- saving -------------------------------------------------------------- */
  async save() {
    const doc = this.docs.get(this.current);
    if (!doc) return;
    if (!BOOT.canEdit) { toast('Saving is switched off in your configuration.'); return; }
    if (!doc.editable) { toast(doc.name + ' was truncated when it was read, so it cannot be saved.'); return; }
    if (!Doc.dirty(doc)) { this.flash('saved', 'done'); return; }

    Ed.stash();
    this.flash('saving\u2026', 'saving');
    let result;
    try {
      result = await api('/api/save', { path: doc.path, content: doc.text, digest: doc.digest });
    } catch (err) {
      this.flash('save failed', 'failed');
      toast('Could not reach the server: ' + err.message);
      return;
    }
    if (!result || !result.ok) {
      this.flash('save failed', 'failed');
      if (result && result.stale) {
        if (confirm(doc.name + ' changed on disk since you opened it.\n\nOverwrite it with what is in the editor?')) {
          doc.digest = '';
          return this.save();
        }
        return;
      }
      toast((result && result.error) || 'That file would not save.');
      return;
    }
    doc.saved = doc.text;
    doc.digest = result.digest || '';
    Ed.markChanges();
    this.flash('saved', 'done');
    this.tabs();
    this.mark(doc.path);
    this.dock.output('saved ' + doc.path + ' \u00b7 ' + result.size + ' bytes');
  },

  flash(text, cls) {
    const node = $('st-save');
    node.textContent = text;
    node.className = cls || '';
    if (cls === 'done') setTimeout(() => { if (node.className === 'done') node.textContent = ''; }, 2200);
  },

  /* A file the agent — or you — just wrote to gets one sweep of light in the
     tree and a line in the mission panel's list. */
  mark(path) {
    this.artifacts.add(path);
    this.tree.beam(path);
    this.mission.artifacts();
  },

  /* -- small commands ------------------------------------------------------ */
  toggleWrap() {
    Ed.wrapped = !Ed.wrapped;
    try { localStorage.setItem('jarvis.ide.wrap', Ed.wrapped ? '1' : '0'); } catch (err) { /* private */ }
    Ed.applyWrap();
  },

  pickLanguage() {
    const doc = this.docs.get(this.current);
    if (!doc) return;
    this.quick.pick({
      placeholder: 'Colour ' + doc.name + ' as\u2026',
      noun: 'languages',
      items: () => L.catalogue().map((item) => ({
        label: item.label, hint: item.key, value: item.key,
      })),
      onPick: (item) => {
        doc.language = item.value;
        Ed.rebuild();
        this.afterLoad(doc);
        Ed.onScroll();
      },
    });
  },

  /* Ctrl+G. A line number rather than a name, so the list is the file's own
     symbols and a bare number jumps straight there. */
  gotoLine() {
    if (!Ed.doc) return;
    const doc = this.docs.get(this.current);
    this.quick.pick({
      placeholder: 'Go to line, or a symbol by name',
      noun: 'symbols',
      empty: 'no symbols; type a line number',
      items: () => L.outline(doc.text, doc.language).map((symbol) => ({
        label: symbol.name, hint: symbol.kind + '  \u00b7  line ' + symbol.line, value: symbol.line,
      })),
      onPick: (item) => Ed.goto(item.value, 0),
    });
    // A number typed into the box is a line number, whatever the list is doing.
    const jump = (event) => {
      if (event.key !== 'Enter') return;
      const line = parseInt(this.quick.input.value.trim(), 10);
      if (!/^\d+$/.test(this.quick.input.value.trim()) || !(line > 0)) return;
      event.preventDefault();
      event.stopPropagation();
      this.quick.input.removeEventListener('keydown', jump, true);
      this.quick.hide();
      Ed.goto(line, 0);
    };
    this.quick.input.addEventListener('keydown', jump, true);
  },

  /* -- panels -------------------------------------------------------------- */
  panels: {
    init() {
      document.querySelectorAll('.abtn[data-panel]').forEach((button) => {
        button.onclick = () => {
          if (button.classList.contains('on') && Ide.sideOpen()) { Ide.setSide(false); return; }
          this.show(button.dataset.panel);
        };
      });
      $('btn-side-hide').onclick = () => Ide.setSide(false);
      $('btn-side-refresh').onclick = () => {
        if (this.at === 'explorer') Ide.tree.reload();
        else if (this.at === 'problems') Ide.problems.run();
        else if (this.at === 'outline') Ide.outline.run();
        else if (this.at === 'search') Ide.search.run();
        Ide.loadIndex(true);
      };
      $('btn-mission-hide').onclick = () => Ide.setMission(false);
      this.show('explorer', true);
    },
    at: 'explorer',
    show(name, quiet) {
      this.at = name;
      document.querySelectorAll('.abtn[data-panel]').forEach((button) => {
        button.classList.toggle('on', button.dataset.panel === name);
      });
      document.querySelectorAll('.spanel').forEach((panel) => {
        panel.classList.toggle('on', panel.id === 'panel-' + name);
      });
      $('side-title').textContent =
        { explorer: 'Explorer', search: 'Search', outline: 'Outline',
          problems: 'Problems', agent: 'Agent' }[name] || name;
      if (!quiet) Ide.setSide(true);
      if (name === 'search') setTimeout(() => $('search-input').focus(), 40);
      if (name === 'outline') Ide.outline.run();
      if (name === 'problems') Ide.problems.run();
    },
  },

  sideOpen() { return !$('ide').classList.contains('no-side') || $('ide').classList.contains('show-side'); },

  setSide(open) {
    const ide = $('ide');
    ide.classList.add('animate');
    const narrow = window.matchMedia('(max-width: 1000px)').matches;
    if (narrow) ide.classList.toggle('show-side', open);
    else ide.classList.toggle('no-side', !open);
    setTimeout(() => { ide.classList.remove('animate'); Ed.drawMap(); }, 360);
  },

  setMission(open) {
    const ide = $('ide');
    ide.classList.add('animate');
    const narrow = window.matchMedia('(max-width: 1240px)').matches;
    if (narrow) ide.classList.toggle('show-mission', open);
    else ide.classList.toggle('no-mission', !open);
    setTimeout(() => { ide.classList.remove('animate'); Ed.drawMap(); }, 360);
  },

  /* -- the workspace tree -------------------------------------------------- */
  tree: {
    open: new Set(),
    init() { this.host = $('etree'); this.load('', this.host, 0); },
    reload() { this.open.clear(); this.host.textContent = ''; this.load('', this.host, 0); },

    async load(path, host, depth) {
      let data;
      try { data = await api('/api/files?path=' + encodeURIComponent(path)); }
      catch (err) { return; }
      if (!data || data.error) return;
      host.textContent = '';
      data.entries.forEach((entry, i) => host.appendChild(this.row(entry, depth, i)));
      if (data.truncated) {
        const note = el('p', 'side-note', 'That directory has more entries than are shown.');
        host.appendChild(note);
      }
    },

    row(entry, depth, i) {
      const wrap = el('div', 'ebranch');
      const row = el('button', 'erow' + (entry.kind === 'dir' ? ' dir' : ''));
      row.type = 'button';
      row.dataset.path = entry.path;
      row.style.paddingLeft = (8 + depth * 12) + 'px';
      row.style.setProperty('--i', String(i));
      row.title = entry.path;

      if (entry.kind === 'dir') row.append(el('span', 'tw', '\u203a'));
      else row.append(el('span', 'tw', ''));
      row.append(icon(entry.kind === 'dir' ? FOLDER : FILE));
      row.append(el('span', 'nm', entry.name));

      const kids = el('div', 'ekids');
      kids.hidden = true;
      wrap.append(row, kids);

      row.onclick = () => {
        if (entry.kind === 'dir') {
          const showing = !kids.hidden;
          kids.hidden = showing;
          row.classList.toggle('open', !showing);
          if (!showing && !kids.childElementCount) this.load(entry.path, kids, depth + 1);
          return;
        }
        Ide.open(entry.path);
        this.mark(entry.path);
      };
      return wrap;
    },

    mark(path) {
      this.host.querySelectorAll('.erow.on').forEach((row) => row.classList.remove('on'));
      const row = this.host.querySelector('.erow[data-path="' + cssEscape(path) + '"]');
      if (row) row.classList.add('on');
    },

    beam(path) {
      const row = this.host.querySelector('.erow[data-path="' + cssEscape(path) + '"]');
      if (!row || REDUCED) return;
      row.classList.remove('touched');
      void row.offsetWidth;
      row.classList.add('touched');
      setTimeout(() => row.classList.remove('touched'), 1200);
    },
  },

  /* -- find and replace ---------------------------------------------------- */
  find: {
    hits: [], at: -1, sensitive: false,
    init() {
      this.input = $('find-input');
      this.input.addEventListener('input', () => this.run());
      this.input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); this.step(event.shiftKey ? -1 : 1); }
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.hide(); }
      });
      $('btn-find-next').onclick = () => this.step(1);
      $('btn-find-prev').onclick = () => this.step(-1);
      $('btn-find-close').onclick = () => this.hide();
      $('btn-find-case').onclick = (event) => {
        this.sensitive = !this.sensitive;
        event.currentTarget.classList.toggle('on', this.sensitive);
        this.run();
      };
      $('btn-replace-one').onclick = () => this.replace(false);
      $('btn-replace-all').onclick = () => this.replace(true);
      $('replace-input').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); this.replace(event.shiftKey); }
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.hide(); }
      });
    },

    show(replacing) {
      if (!Ed.doc) return;
      $('findbar').hidden = false;
      $('replace-row').hidden = !replacing;
      const selected = Ed.input.value.slice(Ed.input.selectionStart, Ed.input.selectionEnd);
      if (selected && selected.indexOf('\n') === -1) this.input.value = selected;
      this.input.focus();
      this.input.select();
      this.run();
    },

    hide() {
      $('findbar').hidden = true;
      this.hits = []; this.at = -1;
      if (Ed.doc) Ed.input.focus({ preventScroll: true });
    },

    run() {
      const needle = this.input.value;
      this.hits = [];
      this.at = -1;
      if (needle && Ed.doc) {
        const haystack = this.sensitive ? Ed.input.value : Ed.input.value.toLowerCase();
        const probe = this.sensitive ? needle : needle.toLowerCase();
        let from = 0;
        while (this.hits.length < 5000) {
          const found = haystack.indexOf(probe, from);
          if (found === -1) break;
          this.hits.push(found);
          from = found + Math.max(1, probe.length);
        }
      }
      const count = $('find-count');
      count.textContent = this.hits.length ? '0/' + this.hits.length : (needle ? 'none' : '0');
      count.classList.toggle('none', !!needle && !this.hits.length);
      if (this.hits.length) this.step(1, true);
    },

    step(direction, first) {
      if (!this.hits.length) return;
      if (first) {
        // Start from where the caret already is rather than from the top.
        const caret = Ed.input.selectionStart;
        this.at = this.hits.findIndex((index) => index >= caret);
        if (this.at === -1) this.at = 0;
      } else {
        this.at = (this.at + direction + this.hits.length) % this.hits.length;
      }
      const at = this.hits[this.at];
      const length = this.input.value.length;
      Ed.input.focus({ preventScroll: true });
      Ed.input.setSelectionRange(at, at + length);
      Ed.goto(Ed.lineAt(at) + 1, at - Ed.startOf(Ed.lineAt(at)));
      Ed.input.setSelectionRange(at, at + length);
      $('find-count').textContent = (this.at + 1) + '/' + this.hits.length;
      // Keep the focus in the box so Enter keeps stepping.
      if (!first) this.input.focus();
    },

    replace(all) {
      if (!Ed.doc || Ed.input.readOnly || !this.hits.length) return;
      const needle = this.input.value;
      const with_ = $('replace-input').value;
      if (!needle) return;
      if (all) {
        const source = Ed.input.value;
        const next = this.sensitive
          ? source.split(needle).join(with_)
          : source.replace(new RegExp(escapeRe(needle), 'gi'), () => with_);
        Ed.put(next, 0, source.length);
        toast('Replaced ' + this.hits.length + '.');
        this.run();
        return;
      }
      const at = this.hits[Math.max(0, this.at)];
      Ed.put(with_, at, at + needle.length);
      this.run();
    },
  },

  /* -- the picker ----------------------------------------------------------
     One fuzzy list, three jobs: open a file by name, choose a language, go to a
     symbol. They are the same interaction — type, narrow, arrow, Enter — and
     building three of it, or reaching for `prompt()` for the other two, would
     be worse in every way than parameterising the one that already exists. */
  quick: {
    matches: [], at: 0, source: null,

    init() {
      this.input = $('quick-input');
      this.list = $('quick-list');
      this.input.addEventListener('input', () => this.run());
      this.input.addEventListener('keydown', (event) => this.keys(event));
      $('quick-wrap').addEventListener('click', (event) => {
        if (event.target.id === 'quick-wrap') this.hide();
      });
    },

    get open() { return !$('quick-wrap').hidden; },

    /* Files, which is what Ctrl+P means and what this is usually for. */
    show() {
      if (!Ide.index) Ide.loadIndex();
      this.pick({
        placeholder: 'Open a file by name',
        items: () => (Ide.index || []).map((path) => ({
          label: path.split('/').pop(), hint: path, value: path,
        })),
        noun: 'files',
        onPick: (item) => Ide.open(item.value),
      });
    },

    pick(options) {
      this.source = options;
      $('quick-wrap').hidden = false;
      this.input.placeholder = options.placeholder || 'Search';
      this.input.value = options.query || '';
      this.input.focus();
      this.input.select();
      this.run();
    },

    hide() {
      $('quick-wrap').hidden = true;
      this.source = null;
    },

    run() {
      if (!this.source) return;
      const query = this.input.value.trim().toLowerCase();
      const items = this.source.items() || [];
      // Matched on the hint when there is one — a path, a signature — because
      // that is what carries the information you are actually typing towards.
      const haystack = items.map((item) => item.hint || item.label);
      this.matches = query
        ? fuzzy(haystack, query, 80).map((hit) => items[hit.index])
        : items.slice(0, 80);
      this.at = 0;

      this.list.textContent = '';
      this.matches.forEach((item, i) => {
        const row = el('li', i ? '' : 'active');
        row.append(el('span', 'qn', item.label));
        if (item.hint && item.hint !== item.label) row.append(el('span', 'qp', item.hint));
        // The callback is captured now rather than read at click time: hiding
        // clears `this.source`, and reading it afterwards would find nothing.
        const onPick = this.source.onPick;
        row.onclick = () => { this.hide(); onPick(item); };
        this.list.appendChild(row);
      });
      $('quick-foot').textContent = items.length
        ? this.matches.length + ' of ' + items.length + ' ' + (this.source.noun || 'items')
        : (this.source.empty || 'nothing to show');
    },

    keys(event) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.hide(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const rows = Array.prototype.slice.call(this.list.children);
        if (!rows.length) return;
        this.at = (this.at + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
        rows.forEach((row, i) => row.classList.toggle('active', i === this.at));
        rows[this.at].scrollIntoView({ block: 'nearest' });
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        const item = this.matches[this.at];
        if (!item || !this.source) return;
        const onPick = this.source.onPick;
        this.hide();
        onPick(item);
      }
    },
  },

  /* -- workspace search ---------------------------------------------------- */
  search: {
    sensitive: false,
    init() {
      this.input = $('search-input');
      this.input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); this.run(); }
        if (event.key === 'Escape') { event.stopPropagation(); this.input.value = ''; }
      });
      let timer = null;
      this.input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => this.run(), 420);
      });
      $('btn-search-case').onclick = (event) => {
        this.sensitive = !this.sensitive;
        event.currentTarget.classList.toggle('on', this.sensitive);
        this.run();
      };
    },

    async run() {
      const query = this.input.value.trim();
      const host = $('search-hits');
      const note = $('search-note');
      host.textContent = '';
      if (query.length < 2) { note.hidden = false; note.textContent = 'Two characters or more.'; return; }
      note.hidden = false;
      note.textContent = 'Searching\u2026';

      let data;
      try {
        data = await api('/api/search?q=' + encodeURIComponent(query) +
                         '&case=' + (this.sensitive ? '1' : '0'));
      } catch (err) { note.textContent = 'The search could not run.'; return; }
      if (!data || data.error) { note.textContent = (data && data.error) || 'The search could not run.'; return; }
      if (!data.files.length) { note.textContent = 'No matches.'; return; }

      note.hidden = true;
      data.files.forEach((file) => {
        const head = el('button', 'hit-file');
        head.type = 'button';
        head.append(el('span', '', file.name), el('span', 'cnt', String(file.matches.length)));
        head.onclick = () => Ide.open(file.path, { line: file.matches[0].line });
        host.appendChild(head);
        file.matches.forEach((match) => {
          const row = el('button', 'hit');
          row.type = 'button';
          row.append(el('span', 'ln', String(match.line)));
          const text = match.text.trim();
          const at = (this.sensitive ? text : text.toLowerCase())
            .indexOf(this.sensitive ? query : query.toLowerCase());
          if (at === -1) row.append(document.createTextNode(text));
          else {
            row.append(document.createTextNode(text.slice(0, at)));
            row.append(el('b', '', text.slice(at, at + query.length)));
            row.append(document.createTextNode(text.slice(at + query.length)));
          }
          row.onclick = () => Ide.open(file.path, { line: match.line, column: match.column });
          host.appendChild(row);
        });
      });
      $('search-note').hidden = true;
      if (data.truncated) host.appendChild(el('p', 'side-note', 'More matches exist than are shown.'));
    },
  },

  /* -- outline ------------------------------------------------------------- */
  outline: {
    init() { this.host = $('outline'); },
    run() {
      const doc = Ide.docs.get(Ide.current);
      const note = $('outline-note');
      this.host.textContent = '';
      if (!doc) { note.hidden = false; note.textContent = 'Open a file to see its shape.'; return; }
      const symbols = L.outline(doc.text, doc.language);
      if (!symbols.length) {
        note.hidden = false;
        note.textContent = 'Nothing this editor recognises as a symbol in ' + L.label(doc.language) + '.';
        return;
      }
      note.hidden = true;
      symbols.forEach((symbol, i) => {
        const row = el('button', 'sym');
        row.type = 'button';
        row.dataset.kind = symbol.kind;
        row.style.setProperty('--d', String(symbol.depth));
        row.style.setProperty('--i', String(i));
        row.append(el('span', 'kind', symbol.kind.slice(0, 1).toUpperCase()));
        row.append(el('span', 'nm', symbol.name));
        row.append(el('span', 'at', String(symbol.line)));
        row.onclick = () => Ed.goto(symbol.line, 0);
        this.host.appendChild(row);
      });
    },
  },

  /* -- problems ------------------------------------------------------------ */
  problems: {
    init() { this.host = $('problems'); this.dockHost = $('dock-problems'); },
    run() {
      const doc = Ide.docs.get(Ide.current);
      const note = $('problems-note');
      this.host.textContent = '';
      this.dockHost.textContent = '';
      Ed.gutter.forEach((node) => { const mark = node.querySelector('.mk'); if (mark) mark.remove(); });

      const found = doc ? L.lint(doc.text, doc.language) : [];
      const errors = found.filter((problem) => problem.level === 'error').length;

      $('st-problem-count').textContent = String(found.length);
      $('st-problems').classList.toggle('has-errors', errors > 0);
      const badge = $('abadge-problems');
      badge.textContent = String(found.length);
      badge.hidden = !found.length;
      const dbadge = $('dbadge');
      dbadge.textContent = String(found.length);
      dbadge.hidden = !found.length;

      if (!found.length) {
        note.hidden = false;
        note.textContent = doc ? 'Nothing to report in ' + doc.name + '.' : 'Nothing to report.';
        this.dockHost.appendChild(el('p', 'side-note', note.textContent));
        return;
      }
      note.hidden = true;
      found.forEach((problem, i) => {
        this.host.appendChild(this.row(problem, i));
        this.dockHost.appendChild(this.row(problem, i));
        const gutter = Ed.gutter[problem.line - 1];
        if (gutter && !gutter.querySelector('.mk')) {
          gutter.appendChild(el('span', 'mk ' + problem.level));
        }
      });
    },
    row(problem, i) {
      const row = el('button', 'prob');
      row.type = 'button';
      row.dataset.level = problem.level;
      row.style.setProperty('--i', String(Math.min(i, 30)));
      row.append(el('span', 'sev'));
      row.append(el('span', 'ptxt', problem.message));
      row.append(el('span', 'at', String(problem.line)));
      row.onclick = () => Ed.goto(problem.line, 0);
      return row;
    },
  },

  /* -- the bottom dock ----------------------------------------------------- */
  dock: {
    at: 'problems',
    init() {
      document.querySelectorAll('.dock-tab').forEach((tab) => {
        tab.onclick = () => this.show(tab.dataset.dock);
      });
      $('btn-dock-hide').onclick = () => this.set(false);
      $('btn-dock-clear').onclick = () => {
        if (this.at === 'output') $('dock-output').textContent = '';
        else if (this.at === 'terminal') $('shell-out').textContent = '';
      };
      try {
        if (localStorage.getItem('jarvis.ide.dock') === '0') this.set(false, true);
      } catch (err) { /* private */ }
    },

    show(name) {
      this.at = name;
      document.querySelectorAll('.dock-tab').forEach((tab) => {
        tab.classList.toggle('on', tab.dataset.dock === name);
      });
      document.querySelectorAll('.dview').forEach((view) => {
        view.classList.toggle('on', view.id === 'dock-' + name);
      });
      this.set(true);
      if (name === 'terminal') setTimeout(() => { Sh.focus(); Sh.toBottom(); }, 60);
    },

    set(open, quiet) {
      $('stage').classList.toggle('no-dock', !open);
      if (!quiet) { try { localStorage.setItem('jarvis.ide.dock', open ? '1' : '0'); } catch (err) { /* private */ } }
      requestAnimationFrame(() => Ed.drawMap());
    },

    toggle() { this.set($('stage').classList.contains('no-dock')); },

    /* One shell, two homes. The pane is moved rather than copied, so the
       scrollback, the history and the running command are the same object
       wherever you happen to be looking at it. */
    reparent(toCode) {
      const host = $('shell-host');
      if (!host) return;
      const target = toCode ? $('dock-terminal') : $('view-terminal');
      if (host.parentNode !== target) target.appendChild(host);
      if (toCode && this.at === 'terminal') setTimeout(() => Sh.toBottom(), 40);
    },

    output(text, level) {
      const host = $('dock-output');
      const row = el('div', 'oline ' + (level || ''));
      row.append(el('span', 'ts', new Date().toLocaleTimeString()));
      row.append(document.createTextNode(text));
      host.appendChild(row);
      while (host.children.length > 600) host.removeChild(host.firstChild);
      host.scrollTop = host.scrollHeight;
    },
  },

  /* -- the mission panel --------------------------------------------------- */
  mission: {
    init() {
      const acts = $('acts');
      ACTIONS.forEach((action) => {
        const button = el('button', 'act', action[0]);
        button.type = 'button';
        button.title = action[2];
        button.onclick = () => this.ask(action[1]);
        acts.appendChild(button);
      });
      $('btn-mission-send').onclick = () => this.send();
      $('mission-input').addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); this.send(); }
        if (event.key === 'Escape') event.stopPropagation();
      });
      this.artifacts();
    },

    scope(selected) {
      $('mission-scope').textContent = selected ? 'the selection' : 'whole file';
    },

    context() {
      const doc = Ide.docs.get(Ide.current);
      if (!doc) return '';
      const selected = Ed.input.value.slice(Ed.input.selectionStart, Ed.input.selectionEnd);
      if (selected.trim()) {
        const from = Ed.lineAt(Ed.input.selectionStart) + 1;
        const to = Ed.lineAt(Ed.input.selectionEnd) + 1;
        return '`' + doc.path + '` lines ' + from + '\u2013' + to + ':\n\n```' +
               doc.language + '\n' + selected + '\n```';
      }
      return '`' + doc.path + '`';
    },

    ask(shape) {
      const doc = Ide.docs.get(Ide.current);
      if (!doc) { toast('Open a file first.'); return; }
      $('mission-input').value = shape;
      this.send();
    },

    send() {
      const box = $('mission-input');
      const question = box.value.trim();
      if (!question) { box.focus(); return; }
      const context = this.context();
      api('/api/chat', { text: context ? question + '\n\n' + context : question })
        .catch((err) => toast('Could not send that: ' + err.message));
      box.value = '';
      this.feed('you asked: ' + question.slice(0, 90), 'user');
    },

    feed(text, kind) {
      const host = $('feed');
      const item = el('div', 'fitem');
      item.dataset.kind = kind || 'info';
      item.append(el('span', 'fdot'), el('span', 'ftxt', text));
      host.appendChild(item);
      while (host.children.length > 80) host.removeChild(host.firstChild);
      host.scrollTop = host.scrollHeight;
    },

    artifacts() {
      const host = $('artifacts');
      host.textContent = '';
      const list = Array.from(Ide.artifacts);
      $('artifacts-note').hidden = list.length > 0;
      list.slice(-24).reverse().forEach((path) => {
        const row = el('button', 'hit-file');
        row.type = 'button';
        row.append(el('span', '', path.split('/').pop()), el('span', 'cnt', 'open'));
        row.title = path;
        row.onclick = () => Ide.open(path);
        host.appendChild(row);
      });
    },
  },

  /* -- seams --------------------------------------------------------------- */
  seams: {
    init() {
      this.drag($('ide-seam-l'), '--side-w', 1, 170, 460);
      this.drag($('ide-seam-r'), '--mission-w', -1, 220, 520);
      this.dragY($('dock-seam'), '--dock-h', 90, 560);
      ['--side-w', '--mission-w', '--dock-h'].forEach((name) => {
        try {
          const saved = localStorage.getItem('jarvis.ide' + name);
          if (saved) $('ide').style.setProperty(name, saved);
        } catch (err) { /* private */ }
      });
    },

    drag(seam, name, direction, low, high) {
      if (!seam) return;
      seam.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        seam.classList.add('active');
        seam.setPointerCapture(event.pointerId);
        const startX = event.clientX;
        const startWidth = parseFloat(getComputedStyle($('ide')).getPropertyValue(name)) || low;
        const move = (moved) => {
          const width = clamp(startWidth + (moved.clientX - startX) * direction, low, high);
          $('ide').style.setProperty(name, width + 'px');
        };
        const up = () => {
          seam.classList.remove('active');
          seam.removeEventListener('pointermove', move);
          seam.removeEventListener('pointerup', up);
          try { localStorage.setItem('jarvis.ide' + name, $('ide').style.getPropertyValue(name)); }
          catch (err) { /* private */ }
          Ed.drawMap();
          if (Ed.wrapped) Ed.measureGutter();
        };
        seam.addEventListener('pointermove', move);
        seam.addEventListener('pointerup', up);
      });
    },

    dragY(seam, name, low, high) {
      if (!seam) return;
      seam.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        seam.classList.add('active');
        seam.setPointerCapture(event.pointerId);
        const startY = event.clientY;
        const startHeight = parseFloat(getComputedStyle($('ide')).getPropertyValue(name)) || low;
        const move = (moved) => {
          const height = clamp(startHeight - (moved.clientY - startY), low, high);
          $('ide').style.setProperty(name, height + 'px');
        };
        const up = () => {
          seam.classList.remove('active');
          seam.removeEventListener('pointermove', move);
          seam.removeEventListener('pointerup', up);
          try { localStorage.setItem('jarvis.ide' + name, $('ide').style.getPropertyValue(name)); }
          catch (err) { /* private */ }
          Ed.drawMap();
        };
        seam.addEventListener('pointermove', move);
        seam.addEventListener('pointerup', up);
      });
    },
  },

  /* -- the specular highlight --------------------------------------------- */
  /* One listener for the whole surface rather than one per pane: the glass only
     needs to know where the pointer is, and a hundred handlers asking the same
     question is a hundred times the work for the same answer. */
  glass() {
    if (REDUCED) return;
    let frame = 0;
    $('ide').addEventListener('pointermove', (event) => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const pane = event.target.closest ? event.target.closest('.glass') : null;
        if (!pane) return;
        const box = pane.getBoundingClientRect();
        pane.style.setProperty('--gx', (event.clientX - box.left) + 'px');
        pane.style.setProperty('--gy', (event.clientY - box.top) + 'px');
      });
    }, { passive: true });
  },

  /* -- the file index ------------------------------------------------------ */
  async loadIndex(force) {
    if (this.index && !force) return;
    try {
      const data = await api('/api/index');
      this.index = (data && data.paths) || [];
      if (this.quick.open) this.quick.run();
    } catch (err) { this.index = []; }
  },

  /* -- listening to the agent ---------------------------------------------- */
  /* The Code surface does not open its own connection: it wraps the handlers
     the conversation already registered, so there is exactly one event stream
     and no chance of the two surfaces disagreeing about what happened. */
  observe() {
    const wrap = (name, extra) => {
      const original = HANDLERS[name];
      HANDLERS[name] = (data) => {
        if (original) original(data);
        try { extra(data); } catch (err) { /* a decoration must never break the stream */ }
      };
    };
    wrap('tool_start', (d) => {
      this.mission.feed(d.name + (d.arguments ? ' ' + short(JSON.stringify(d.arguments)) : ''), 'tool');
      this.dock.output(d.name + ' \u2026');
      this.noticeFile(d.arguments);
    });
    wrap('tool_end', (d) => {
      this.mission.feed(d.name + ' \u2192 ' + short(d.summary || (d.ok ? 'done' : 'failed')), d.ok ? 'tool' : 'error');
      this.dock.output(d.name + ' \u2192 ' + (d.summary || (d.ok ? 'done' : 'failed')), d.ok ? '' : 'error');
    });
    wrap('system', (d) => {
      if (d.level === 'warn' || d.level === 'error') this.dock.output(d.text, d.level);
    });
    wrap('agent', (d) => this.mission.feed(short(flat(d.text), 130), 'info'));
    wrap('state', (d) => {
      $('mission-label').textContent =
        { idle: 'Idle', thinking: 'Thinking', working: 'Working',
          listening: 'Listening', speaking: 'Speaking' }[d.state] || d.state;
    });
    wrap('code', (d) => { if (d.title) this.dock.output('rendered ' + d.title); });
  },

  /* A tool argument that names a file in this workspace is worth noticing: it
     is how the agent tells you, without being asked, which file it is working
     on. */
  noticeFile(args) {
    if (!args || typeof args !== 'object') return;
    const candidate = args.path || args.file || args.filename || args.target;
    if (typeof candidate !== 'string' || !candidate) return;
    const path = candidate.replace(/^\.\//, '');
    this.mark(path);
    const doc = this.docs.get(path);
    if (doc) this.refresh(path);
  },

  /* A file the agent just rewrote, reloaded under the editor — unless you have
     unsaved changes in it, in which case the editor says so and leaves your
     work alone. */
  async refresh(path) {
    const doc = this.docs.get(path);
    if (!doc) return;
    if (Doc.dirty(doc)) {
      this.dock.output(doc.name + ' changed on disk, and you have unsaved changes here', 'warn');
      toast(doc.name + ' changed on disk. Your version is still here, unsaved.');
      return;
    }
    let data;
    try { data = await api('/api/file?path=' + encodeURIComponent(path)); }
    catch (err) { return; }
    if (!data || data.error || data.binary) return;
    if (data.digest === doc.digest) return;
    doc.text = data.content;
    doc.saved = data.content;
    doc.digest = data.digest;
    if (this.current === path) {
      const caret = Ed.input.selectionStart;
      Ed.load(doc);
      Ed.select(Math.min(caret, doc.text.length));
    }
    this.dock.output('reloaded ' + path);
  },

  /* -- keyboard ------------------------------------------------------------ */
  keys() {
    document.addEventListener('keydown', (event) => {
      const meta = event.ctrlKey || event.metaKey;
      if (this.quick.open) return;          // its own handler owns the keys

      // Surface switching works from either side.
      if (meta && event.shiftKey && event.key.toLowerCase() === 'c') {
        event.preventDefault();
        this.surface(this.showing ? 'chat' : 'code');
        return;
      }
      if (!this.showing) return;

      if (meta && !event.shiftKey && event.key.toLowerCase() === 'p') {
        event.preventDefault(); this.quick.show(); return;
      }
      if (meta && event.shiftKey) {
        const lower = event.key.toLowerCase();
        if (lower === 'e') { event.preventDefault(); this.panels.show('explorer'); return; }
        if (lower === 'f') { event.preventDefault(); this.panels.show('search'); return; }
        if (lower === 'o') { event.preventDefault(); this.panels.show('outline'); return; }
        if (lower === 'm') { event.preventDefault(); this.panels.show('problems'); this.dock.show('problems'); return; }
        if (lower === 'a') { event.preventDefault(); this.panels.show('agent'); return; }
        if (lower === 'b') { event.preventDefault(); this.setMission($('ide').classList.contains('no-mission')); return; }
      }
      if (meta && event.key.toLowerCase() === 'b' && !event.shiftKey) {
        event.preventDefault(); this.setSide(!this.sideOpen()); return;
      }
      if (meta && event.key.toLowerCase() === 'j') { event.preventDefault(); this.dock.toggle(); return; }
      if (meta && (event.key === '`' || event.key === '~')) {
        event.preventDefault(); this.dock.show('terminal'); return;
      }
      // Alt+W, not Ctrl+W: Ctrl+W closes the window in every browser there is,
      // and an editor that can silently lose the whole session to a typo is not
      // one you would leave a file open in.
      if (event.altKey && event.key.toLowerCase() === 'w' && this.current) {
        event.preventDefault(); this.close(this.current); return;
      }
      if (event.key === 'Escape' && !$('findbar').hidden) {
        event.preventDefault(); this.find.hide();
      }
    }, true);
  },
};

/* The six things worth asking about a file, phrased the way you would ask a
   colleague rather than the way you would prompt a model. */
const ACTIONS = [
  ['Explain', 'Walk me through what this does, and why it is written this way.', 'Read it back to me'],
  ['Review', 'Review this for bugs, edge cases and anything that would fail in production.', 'Look for trouble'],
  ['Simplify', 'Simplify this without changing its behaviour. Show me the diff and say what you removed.', 'Cut what is not earning its place'],
  ['Tests', 'Write tests for this. Cover the edge cases, not just the happy path.', 'Cover it'],
  ['Document', 'Write the docstrings and comments this needs. Explain why, not what.', 'Say what it is for'],
  ['Fix', 'Something here is wrong. Find it and tell me how to fix it.', 'Find the bug'],
];

const FOLDER = 'M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z';
const FILE = 'M6 3h7l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM13 3v5h5';

function icon(path) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  shape.setAttribute('d', path);
  svg.appendChild(shape);
  return svg;
}

/* An attribute selector has to survive a path with a quote in it. */
function cssEscape(text) {
  if (window.CSS && CSS.escape) return CSS.escape(text).replace(/\\\//g, '/');
  return String(text).replace(/["\\]/g, '\\$&');
}

const short = (text, limit) => {
  const value = String(text == null ? '' : text);
  return value.length > (limit || 90) ? value.slice(0, limit || 90) + '\u2026' : value;
};
const flat = (text) => String(text == null ? '' : text).replace(/\s+/g, ' ').trim();

/* The indent width is the commonest *step* between one line and the next, not
   the commonest absolute indent. Counting absolute indents reports 8 for any
   Python file with more nesting than top-level statements, which is both wrong
   and the first thing anybody notices in the status bar. */
function indentStyle(text) {
  const lines = String(text).split('\n').slice(0, 800);
  let tabs = 0;
  let spaced = 0;
  const steps = {};
  let previous = 0;
  lines.forEach((line) => {
    if (!line.trim()) return;
    if (/^\t/.test(line)) { tabs += 1; return; }
    const indent = line.match(/^ */)[0].length;
    if (indent > 0) spaced += 1;
    const step = indent - previous;
    if (step > 0 && step <= 8) steps[step] = (steps[step] || 0) + 1;
    previous = indent;
  });
  if (tabs > spaced) return 'Tabs';
  const common = Object.keys(steps).sort((a, b) => steps[b] - steps[a])[0];
  return 'Spaces: ' + (common || 4);
}

/* Subsequence matching, weighted so a run of characters and a hit on the file
   name itself both count for more than a scatter across the directory. */
function fuzzy(paths, query, limit) {
  const out = [];
  for (let p = 0; p < paths.length; p++) {
    const path = String(paths[p]);
    const hay = path.toLowerCase();
    let score = 0;
    let at = 0;
    let run = 0;
    let ok = true;
    for (let q = 0; q < query.length; q++) {
      const found = hay.indexOf(query[q], at);
      if (found === -1) { ok = false; break; }
      run = found === at ? run + 1 : 0;
      score += 10 + run * 6 - Math.min(8, found - at);
      at = found + 1;
    }
    if (!ok) continue;
    const name = path.slice(path.lastIndexOf('/') + 1).toLowerCase();
    if (name.indexOf(query) !== -1) score += 60;
    if (name.indexOf(query) === 0) score += 40;
    score -= path.length * 0.12;
    out.push({ path: path, score: score, index: p });
    if (out.length > 4000) break;
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, limit || 60);
}

window.Ide = Ide;
window.IdeEd = Ed;

function start() {
  try { Ide.init(); } catch (err) {
    console.error('The Code surface would not start', err);
    return;
  }
  let remembered = 'chat';
  try { remembered = localStorage.getItem('jarvis.surface') || 'chat'; } catch (err) { /* private */ }
  Ide.surface(remembered === 'code' ? 'code' : 'chat', { quiet: true });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();

}());
