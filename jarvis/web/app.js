/* =============================================================================
   J.A.R.V.I.S. Desktop -- the front end.

   No framework, no build step, no dependencies. The whole application is one
   file because it is genuinely small: a Server-Sent Events connection, a
   transcript that appends nodes, a composer, and a colour studio.

   Everything that touches the screen obeys two rules.

   Text from the model is *never* trusted as markup. `esc()` runs first, always,
   and the Markdown renderer works on the escaped string. A model that emits a
   script tag gets a script tag rendered as text, which is what an assistant
   that also runs shell commands requires.

   And the interface owns no colours. Every visible colour is a CSS custom
   property the server derived from the operator's seed; changing a theme means
   writing new values into one style element and letting CSS transitions do the
   rest.
   ============================================================================= */
'use strict';

const BOOT = window.JARVIS_BOOT || {};
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

/* The inverse of esc(), for the one place the highlighter has to hand a slice
   of already-escaped text back through itself. */
const unesc = (s) => String(s == null ? '' : s)
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');

/* Escape before anything else touches model output. */
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const state = {
  token: BOOT.token || new URLSearchParams(location.search).get('token') || '',
  theme: BOOT.theme || {},
  presets: BOOT.presets || [],
  busy: false,
  streaming: null,      // the .body element currently receiving tokens
  streamBuffer: '',
  atBottom: true,
  lastTool: null,
};

/* -- transport --------------------------------------------------------------
   Every request carries the session token. Without it the server answers 403,
   which is the whole point: this port can run shell commands. */
async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Jarvis-Token': state.token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok && res.status !== 202) throw new Error(res.status + ' ' + res.statusText);
  return res.status === 204 ? null : res.json().catch(() => null);
}

/* =============================================================================
   Colour
   ============================================================================= */
const Colour = {
  toRgb(hex) {
    let h = String(hex || '').replace('#', '').trim();
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    if (!/^[0-9a-f]{6}$/i.test(h)) return null;
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  },
  toHex(r, g, b) {
    return '#' + [r, g, b].map((c) => clamp(Math.round(c), 0, 255).toString(16).padStart(2, '0')).join('');
  },
  toHsl(hex) {
    const rgb = Colour.toRgb(hex);
    if (!rgb) return null;
    const [r, g, b] = rgb.map((c) => c / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    const l = (max + min) / 2;
    if (!d) return [0, 0, l];
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
    return [h, s, l];
  },
  fromHsl(h, s, l) {
    h = ((h % 1) + 1) % 1;
    if (!s) { const v = Math.round(l * 255); return Colour.toHex(v, v, v); }
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const hue = (t) => {
      t = ((t % 1) + 1) % 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    return Colour.toHex(hue(h + 1 / 3) * 255, hue(h) * 255, hue(h - 1 / 3) * 255);
  },
  valid: (hex) => Colour.toRgb(hex) !== null,
};

/* =============================================================================
   Theme studio

   The wheel is hue around, saturation outward. Lightness is the slider beneath
   it, because putting all three on one surface is how colour pickers become
   unusable. Dragging is throttled to one frame: the local preview is instant,
   and the server is told at animation-frame cadence, never per pointer event.
   ============================================================================= */
const Studio = {
  open: false,
  pending: null,
  frame: 0,

  init() {
    this.buildModes();
    this.buildFonts();
    this.buildSwatches();
    this.drawWheel();
    this.bind();
    this.sync(state.theme);
  },

  /* -- the wheel ---------------------------------------------------------- */
  drawWheel() {
    const canvas = $('wheel');
    const ctx = canvas.getContext('2d');
    const size = canvas.width, mid = size / 2;
    const image = ctx.createImageData(size, size);
    const data = image.data;
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const dx = x - mid, dy = y - mid;
        const dist = Math.sqrt(dx * dx + dy * dy) / mid;
        const i = (y * size + x) * 4;
        if (dist > 1) { data[i + 3] = 0; continue; }
        const hue = (Math.atan2(dy, dx) / (Math.PI * 2) + 1) % 1;
        const rgb = Colour.toRgb(Colour.fromHsl(hue, clamp(dist, 0, 1), 0.5));
        data[i] = rgb[0]; data[i + 1] = rgb[1]; data[i + 2] = rgb[2];
        // Feather the last two pixels so the rim is not a staircase.
        data[i + 3] = dist > 0.985 ? Math.round(255 * (1 - (dist - 0.985) / 0.015)) : 255;
      }
    }
    ctx.putImageData(image, 0, 0);
  },

  pointAt(hex) {
    const hsl = Colour.toHsl(hex);
    if (!hsl) return { x: 50, y: 50 };
    const angle = hsl[0] * Math.PI * 2;
    const radius = clamp(hsl[1], 0, 1) * 47;   // 47% keeps the thumb inside the rim
    return { x: 50 + Math.cos(angle) * radius, y: 50 + Math.sin(angle) * radius };
  },

  fromPoint(event) {
    const rect = $('wheel').getBoundingClientRect();
    const mid = rect.width / 2;
    const dx = event.clientX - rect.left - mid;
    const dy = event.clientY - rect.top - mid;
    const hue = (Math.atan2(dy, dx) / (Math.PI * 2) + 1) % 1;
    const sat = clamp(Math.sqrt(dx * dx + dy * dy) / mid, 0, 1);
    const light = Number($('s-light').value) / 100;
    return Colour.fromHsl(hue, sat, light);
  },

  /* -- controls ----------------------------------------------------------- */
  buildModes() {
    const group = $('mode-group');
    group.innerHTML = '';
    const labels = { dark: 'Dark', midnight: 'Midnight', light: 'Light' };
    (BOOT.modes || ['dark', 'midnight', 'light']).forEach((mode) => {
      const button = el('button', '', labels[mode] || mode);
      button.dataset.mode = mode;
      button.setAttribute('role', 'radio');
      button.onclick = () => Studio.push({ mode: mode });
      group.appendChild(button);
    });
  },

  buildFonts() {
    const group = $('font-group');
    group.innerHTML = '';
    (BOOT.fonts || []).forEach((font) => {
      const button = el('button', '', font.label);
      button.dataset.font = font.key;
      button.setAttribute('role', 'radio');
      button.onclick = () => Studio.push({ font: font.key });
      group.appendChild(button);
    });
  },

  buildSwatches() {
    const grid = $('swatches');
    grid.innerHTML = '';
    state.presets.forEach((preset) => {
      const button = el('button', 'swatch');
      button.dataset.preset = preset.key;
      button.title = preset.name;
      button.setAttribute('aria-label', 'Use the ' + preset.name + ' theme');
      // A miniature of the theme itself: its ground, its accent, its name.
      const hsl = Colour.toHsl(preset.seed) || [0, 0, 0.5];
      const ground = preset.mode === 'light'
        ? Colour.fromHsl(hsl[0], Math.min(hsl[1], 0.55) * 0.3 * preset.tint, 0.985 - 0.03 * preset.tint)
        : Colour.fromHsl(hsl[0], Math.min(hsl[1], 0.85) * 0.42 * preset.tint,
                         preset.mode === 'midnight' ? 0.03 : 0.055 + 0.022 * preset.tint);
      const bg = el('span', 'sw-bg');
      bg.style.background = 'radial-gradient(circle at 30% 25%, ' + preset.seed + '44, ' + ground + ' 72%)';
      const dot = el('span', 'sw-dot');
      dot.style.background = preset.seed;
      dot.style.color = preset.seed;
      button.append(bg, dot, el('span', 'sw-name', preset.name));
      button.onclick = () => Studio.push({ preset: preset.key });
      grid.appendChild(button);
    });
  },

  bind() {
    const wheel = $('wheel'), thumb = $('wheel-thumb');
    let dragging = false;
    const move = (event) => {
      if (!dragging) return;
      event.preventDefault();
      const hex = this.fromPoint(event);
      this.preview(hex);
      this.push({ seed: hex }, true);
    };
    wheel.addEventListener('pointerdown', (event) => {
      dragging = true;
      thumb.classList.add('dragging');
      wheel.setPointerCapture(event.pointerId);
      move(event);
    });
    wheel.addEventListener('pointermove', move);
    const release = () => {
      if (!dragging) return;
      dragging = false;
      thumb.classList.remove('dragging');
      this.flush();                     // one authoritative save at the end
    };
    wheel.addEventListener('pointerup', release);
    wheel.addEventListener('pointercancel', release);

    $('s-light').addEventListener('input', () => {
      this.readouts();
      const hsl = Colour.toHsl(state.theme.seed) || [0.5, 0.7, 0.55];
      const hex = Colour.fromHsl(hsl[0], hsl[1], Number($('s-light').value) / 100);
      this.preview(hex);
      this.push({ seed: hex }, true);
    });
    $('s-light').addEventListener('change', () => this.flush());

    [['s-tint', 'tint'], ['s-contrast', 'contrast'], ['s-glow', 'glow']].forEach((pair) => {
      const id = pair[0], key = pair[1];
      $(id).addEventListener('input', () => {
        const changes = {};
        changes[key] = Number($(id).value) / 100;
        this.readouts();
        this.push(changes, true);
      });
      $(id).addEventListener('change', () => this.flush());
    });
    $('s-radius').addEventListener('input', () => {
      this.readouts();
      this.push({ radius: Number($('s-radius').value) }, true);
    });
    $('s-radius').addEventListener('change', () => this.flush());

    const hexField = $('hex'), picker = $('picker');
    hexField.addEventListener('change', () => {
      const value = hexField.value.trim();
      if (Colour.valid(value)) this.push({ seed: value.startsWith('#') ? value : '#' + value });
      else { toast('That is not a colour I can read.'); this.sync(state.theme); }
    });
    picker.addEventListener('input', () => { this.preview(picker.value); this.push({ seed: picker.value }, true); });
    picker.addEventListener('change', () => this.flush());

    $('hex2').addEventListener('change', () => {
      const value = $('hex2').value.trim();
      if (Colour.valid(value)) this.push({ secondary: value.startsWith('#') ? value : '#' + value });
    });
    $('picker2').addEventListener('input', () => this.push({ secondary: $('picker2').value }, true));
    $('picker2').addEventListener('change', () => this.flush());

    $('btn-surprise').onclick = () => this.push({ surprise: true });
    $('btn-studio-close').onclick = () => this.hide();
  },

  /* -- preview and push ----------------------------------------------------
     Local, instant feedback while a pointer is down: paint the accent
     immediately so the wheel feels connected to the interface, then let the
     server's authoritative derivation land a frame later. */
  preview(hex) {
    if (!Colour.valid(hex)) return;
    document.documentElement.style.setProperty('--accent', hex);
    const thumb = $('wheel-thumb');
    thumb.style.background = hex;
    $('hex').value = hex;
    $('picker').value = hex;
    const point = this.pointAt(hex);
    thumb.style.left = point.x + '%';
    thumb.style.top = point.y + '%';
  },

  /* `live` coalesces a drag into one request per animation frame. Without it a
     pointermove storm would put a hundred requests a second on the wire. */
  push(changes, live) {
    this.pending = Object.assign(this.pending || {}, changes);
    if (!live) return this.flush();
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.flush(); });
  },

  flush() {
    if (this.frame) { cancelAnimationFrame(this.frame); this.frame = 0; }
    const body = this.pending;
    this.pending = null;
    if (!body) return;
    api('/api/theme', body).catch((err) => toast('The colour would not stick: ' + err.message));
  },

  /* -- reflect server state ----------------------------------------------- */
  sync(theme) {
    if (!theme) return;
    state.theme = theme;
    $('theme-name').textContent = theme.name || 'Custom';
    $('hex').value = theme.seed || '';
    $('picker').value = theme.seed || '#000000';
    $('hex2').value = theme.secondary || '';
    $('picker2').value = theme.secondary || '#000000';

    const hsl = Colour.toHsl(theme.seed);
    if (hsl) $('s-light').value = Math.round(hsl[2] * 100);
    $('s-tint').value = Math.round((theme.tint == null ? 0.10 : theme.tint) * 100);
    $('s-contrast').value = Math.round((theme.contrast == null ? 0.55 : theme.contrast) * 100);
    $('s-glow').value = Math.round((theme.glow == null ? 0.10 : theme.glow) * 100);
    $('s-radius').value = theme.radius == null ? 8 : theme.radius;
    this.readouts();

    const point = this.pointAt(theme.seed);
    const thumb = $('wheel-thumb');
    thumb.style.left = point.x + '%';
    thumb.style.top = point.y + '%';
    thumb.style.background = theme.seed;

    $('mode-group').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.mode === theme.mode));
    $('font-group').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.font === theme.font));
    $('swatches').querySelectorAll('.swatch').forEach((b) => {
      const preset = state.presets.find((p) => p.key === b.dataset.preset);
      b.classList.toggle('on', !!preset && preset.seed === theme.seed && preset.mode === theme.mode);
    });
    document.documentElement.dataset.mode = theme.mode || 'dark';
  },

  /* Every slider says what it is set to. A control whose value you can only
     infer from the position of a dot is half a control. */
  readouts() {
    [['s-light', 'v-light', '%'], ['s-tint', 'v-tint', '%'], ['s-contrast', 'v-contrast', '%'],
     ['s-glow', 'v-glow', '%'], ['s-radius', 'v-radius', 'px']].forEach((row) => {
      const out = $(row[1]);
      if (out) out.textContent = $(row[0]).value + row[2];
    });
  },

  /* -- visibility ---------------------------------------------------------- */
  show() {
    this.open = true;
    const panel = $('studio');
    panel.hidden = false;
    panel.classList.remove('closing');
    $('scrim').hidden = false;
    $('btn-palette').classList.add('on');
    setTimeout(() => $('hex').focus({ preventScroll: true }), 260);
  },
  hide() {
    if (!this.open) return;
    this.open = false;
    const panel = $('studio');
    panel.classList.add('closing');
    $('scrim').hidden = true;
    $('btn-palette').classList.remove('on');
    const done = () => { panel.hidden = true; panel.classList.remove('closing'); };
    if (REDUCED) done(); else setTimeout(done, 240);
  },
  toggle() { if (this.open) this.hide(); else this.show(); },
};

/* Write the server's derived token set into the page. One assignment per
   variable; CSS transitions animate the change across the whole interface. */
function applyVariables(variables) {
  if (!variables) return;
  const root = document.documentElement;
  Object.keys(variables).forEach((name) => root.style.setProperty(name, variables[name]));
}

/* =============================================================================
   Markdown

   Deliberately small: headings, emphasis, code, lists, quotes, rules and links.
   It runs on already-escaped text, so the worst a malformed document can do is
   look wrong.
   ============================================================================= */
function markdown(source) {
  const blocks = [];
  /* Fenced code is lifted out first so nothing inside it is interpreted.
     The placeholder is delimited by NUL rather than spaces: the block loop
     below trims every line, and a whitespace-delimited marker would not
     survive that. NUL cannot appear in the escaped text, so it cannot collide
     with anything the model wrote. */
  const text = esc(source).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push({ lang: lang || '', code: code.replace(/\n$/, '') });
    return '\u0000' + (blocks.length - 1) + '\u0000';
  });

  const renderCode = (index) => {
    const block = blocks[Number(index)];
    if (!block) return '';
    return '<div class="codeblock"><div class="codeblock-head"><span>' +
      esc(block.lang || 'text') +
      '</span><button class="copy" type="button">Copy</button></div><pre><code>' +
      block.code + '</code></pre></div>';
  };

  const inline = (s) => s
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>')
    // Only http(s) links become anchors: a javascript: URL never gets the chance.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  const out = [];
  let list = null, quote = [], paragraph = [];

  const closeParagraph = () => {
    if (paragraph.length) { out.push('<p>' + inline(paragraph.join(' ')) + '</p>'); paragraph = []; }
  };
  const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
  const closeQuote = () => {
    if (quote.length) { out.push('<blockquote>' + inline(quote.join(' ')) + '</blockquote>'); quote = []; }
  };
  const closeAll = () => { closeParagraph(); closeList(); closeQuote(); };

  text.split('\n').forEach((raw) => {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { closeAll(); return; }

    const fenced = line.trim().match(/^\u0000(\d+)\u0000$/);
    if (fenced) { closeAll(); out.push(renderCode(fenced[1])); return; }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeAll();
      const level = Math.min(heading[1].length + 1, 4);
      out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
      return;
    }
    if (/^(---|\*\*\*|___)\s*$/.test(line)) { closeAll(); out.push('<hr>'); return; }

    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      closeParagraph(); closeQuote();
      const want = bullet ? 'ul' : 'ol';
      if (list !== want) { closeList(); out.push('<' + want + '>'); list = want; }
      out.push('<li>' + inline((bullet || numbered)[1]) + '</li>');
      return;
    }
    const quoted = line.match(/^&gt;\s?(.*)$/);
    if (quoted) { closeParagraph(); closeList(); quote.push(quoted[1]); return; }

    closeList(); closeQuote();
    paragraph.push(line.trim());
  });
  closeAll();

  // Anything left is a fence that appeared mid-sentence rather than on its own
  // line. Rare, but it must not survive to the screen as a control character.
  return out.join('\n').replace(/\u0000(\d+)\u0000/g, (_, i) => renderCode(i));
}

function wireCodeBlocks(scope) {
  scope.querySelectorAll('.copy').forEach((button) => {
    if (button.dataset.wired) return;
    button.dataset.wired = '1';
    button.onclick = async () => {
      const code = button.closest('.codeblock').querySelector('code').textContent;
      try {
        await navigator.clipboard.writeText(code);
        button.textContent = 'Copied';
        button.classList.add('done');
        setTimeout(() => { button.textContent = 'Copy'; button.classList.remove('done'); }, 1600);
      } catch (err) { toast('The clipboard refused that.'); }
    };
  });
}

/* =============================================================================
   Transcript
   ============================================================================= */
const Transcript = {
  node: null,

  init() {
    this.node = $('transcript');
    this.node.addEventListener('scroll', () => {
      const gap = this.node.scrollHeight - this.node.scrollTop - this.node.clientHeight;
      state.atBottom = gap < 90;
      $('jump').hidden = state.atBottom;
    }, { passive: true });
    $('jump').onclick = () => this.toBottom(true);
  },

  append(node) {
    const welcome = $('welcome');
    if (welcome) welcome.remove();
    this.node.appendChild(node);
    this.toBottom();
    return node;
  },

  /* Only follow the stream when the operator is already at the bottom. Yanking
     someone back down while they are reading is the single rudest thing a chat
     interface can do. */
  toBottom(force) {
    if (!force && !state.atBottom) return;
    requestAnimationFrame(() => {
      this.node.scrollTo({
        top: this.node.scrollHeight,
        behavior: (REDUCED || force) ? 'auto' : 'smooth',
      });
    });
  },

  message(who, text, asMarkdown) {
    const row = el('div', 'msg ' + who);
    row.append(el('div', 'avatar', who === 'user' ? 'YOU' : 'J'));
    const bubble = el('div', 'bubble');
    const body = el('div', 'body');
    if (asMarkdown) { body.innerHTML = markdown(text); wireCodeBlocks(body); }
    else body.textContent = text;
    bubble.appendChild(body);
    row.appendChild(bubble);
    this.append(row);
    return body;
  },

  note(text, level) {
    // Escapes rather than literals: this file stays pure ASCII on disk.
    const glyphs = { info: '\u24D8', warn: '\u26A0', error: '\u2715', success: '\u2713' };
    const row = el('div', 'note ' + (level || 'info'));
    row.append(el('span', 'glyph', glyphs[level] || glyphs.info), el('span', '', text));
    return this.append(row);
  },

  thought(text) { return this.append(el('div', 'thought', text)); },

  /* Tables arrive from /help, /tools, /diag and the like. Built node by node
     rather than through the Markdown renderer: the cells are already plain
     strings, and textContent is the shortest path to not trusting them. */
  table(title, columns, rows) {
    const wrap = el('div', 'note table-note');
    const block = el('div', 'table-block');
    if (title) block.appendChild(el('div', 'table-title', title));
    const table = el('table');
    if ((columns || []).length) {
      const head = el('tr');
      columns.forEach((name) => head.appendChild(el('th', '', name)));
      table.appendChild(el('thead')).appendChild(head);
    }
    const body = el('tbody');
    (rows || []).forEach((row) => {
      const tr = el('tr');
      row.forEach((cell) => tr.appendChild(el('td', '', cell)));
      body.appendChild(tr);
    });
    table.appendChild(body);
    block.appendChild(table);
    wrap.appendChild(block);
    return this.append(wrap);
  },

  toolStart(name, args) {
    const card = el('div', 'tool');
    const head = el('div', 'tool-head');
    head.append(el('span', 'tool-spinner'), el('span', 'tool-name', name));
    card.appendChild(head);
    const detail = (args && Object.keys(args).length) ? JSON.stringify(args) : '';
    if (detail) card.appendChild(el('div', 'tool-args', detail.length > 220 ? detail.slice(0, 219) + '...' : detail));
    state.lastTool = card;
    return this.append(card);
  },

  toolEnd(name, ok, summary) {
    const card = state.lastTool;
    if (!card) return;
    card.classList.add(ok ? 'done' : 'failed');
    if (summary) card.appendChild(el('div', 'tool-out', summary));
    state.lastTool = null;
    this.toBottom();
  },

  clear() {
    this.node.innerHTML = '';
    state.streaming = null;
    state.streamBuffer = '';
  },
};

/* -- streaming --------------------------------------------------------------
   Tokens arrive faster than a full Markdown re-render can keep up with, so the
   stream shows plain text with a caret while it runs, and the block is rendered
   as Markdown once, at the end. That is also why code blocks never appear
   half-fenced mid-answer. */
function streamBegin() {
  state.streamBuffer = '';
  state.streaming = Transcript.message('agent', '', false);
  state.streaming.appendChild(el('span', 'caret'));
}

function streamToken(token) {
  if (!state.streaming) streamBegin();
  state.streamBuffer += token;
  state.streaming.textContent = state.streamBuffer;
  state.streaming.appendChild(el('span', 'caret'));
  Transcript.toBottom();
}

function streamEnd(finalText, interim) {
  if (!state.streaming) {
    if (finalText && !interim) Transcript.message('agent', finalText, true);
    return;
  }
  const body = state.streaming;
  const text = finalText || state.streamBuffer;
  state.streaming = null;
  state.streamBuffer = '';
  if (!text.trim()) {
    const row = body.closest('.msg');
    if (row) row.remove();
    return;
  }
  body.innerHTML = markdown(text);
  wireCodeBlocks(body);
  Transcript.toBottom();
}

/* =============================================================================
   Live connection
   ============================================================================= */
const HANDLERS = {
  user: (d) => {
    Transcript.message('user', d.text, false);
    Term.user(d.text);
    Sessions.name(d.text);
  },
  agent: (d) => {
    Transcript.message('agent', d.text, d.markdown !== false);
    Term.line('agent', BULLET, Term.flatten(d.text));
  },
  system: (d) => {
    Transcript.note(d.text, d.level);
    Term.system(d.text, d.level);
    Logs.add(d.level === 'success' ? 'info' : (d.level || 'info'), d.text);
  },
  interim: (d) => { if (d.text) Transcript.thought(d.text); },
  thought: (d) => { if (d.text) { Transcript.thought(d.text); Term.thought(d.text); } },
  tool_start: (d) => {
    Transcript.toolStart(d.name, d.arguments);
    Term.toolStart(d.name, d.arguments);
    Logs.add('tool', d.name + ' ' + (d.arguments ? JSON.stringify(d.arguments) : ''));
  },
  tool_end: (d) => {
    Transcript.toolEnd(d.name, d.ok, d.summary);
    Term.toolEnd(d.name, d.ok, d.summary);
    Logs.add(d.ok ? 'tool' : 'error', d.name + ' → ' + (d.summary || (d.ok ? 'done' : 'failed')));
  },
  alert: (d) => {
    const level = d.severity === 'critical' ? 'error' : 'warn';
    Transcript.note(d.title + ': ' + d.text, level);
    Logs.add(level, d.title + ': ' + d.text);
    toast(d.title);
  },
  code: (d) => Transcript.message('agent', '```' + (d.language || '') + '\n' + d.code + '\n```', true),
  table: (d) => Transcript.table(d.title, d.columns, d.rows),
  clear: () => { Transcript.clear(); $('term').innerHTML = ''; },
  stream_begin: () => { streamBegin(); Term.streamBegin(); },
  token: (d) => { streamToken(d.text); Term.streamToken(d.text); },
  stream_end: (d) => { streamEnd(d.text, d.interim); Term.streamEnd(d.text); },
  state: (d) => setState(d.state),
  telemetry: (d) => Gauges.update(d),
  metrics: (d) => {
    if (!d.summary) return;
    $('sb-metrics').textContent = d.summary;
    Logs.add('info', 'turn: ' + d.summary);
  },
  model_status: (d) => {
    $('sb-model').textContent = d.text;
    $('chip-model').textContent = String(d.text).split('·')[0].trim();
    const ready = /ready|warm/i.test(d.text);
    $('chip-dot').className = 'chip-dot ' + (ready ? 'ready' : 'down');
    facts('model-facts', [
      ['Model', BOOT.model || '—'],
      ['Host', BOOT.host || '—'],
      ['Status', d.text],
      ['Mode', BOOT.standalone ? 'standalone' : 'attached to a terminal'],
    ]);
    Logs.add(ready ? 'info' : 'warn', d.text);
  },
  voice_status: (d) => Logs.add('info', d.text),
  protocol: (d) => { if (d.name) { toast('Protocol: ' + d.name); Logs.add('info', 'protocol: ' + d.name); } },
  warm: () => {},
  amplitude: () => {},
  theme: (d) => { applyVariables(d.variables); Studio.sync(d.theme); },
  ask: (d) => Ask.card(d),
  ask_done: (d) => Ask.close(d.token),
  shell_out: (d) => Sh.onOutput(d.text),
  shell_done: (d) => Sh.onDone(d.code, d.cwd),
  shell_exit: (d) => Sh.onExit(d.code),
  shutdown: () => {
    Transcript.note('J.A.R.V.I.S. has shut down. You can close this window.', 'warn');
    setState('idle');
  },
};

let source = null;
let retryDelay = 700;

function connect() {
  if (source) source.close();
  source = new EventSource('/api/events?token=' + encodeURIComponent(state.token));

  Object.keys(HANDLERS).forEach((kind) => {
    source.addEventListener(kind, (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (err) { return; }
      try { HANDLERS[kind](payload); } catch (err) { console.error(kind, err); }
    });
  });

  source.onopen = () => {
    retryDelay = 700;
    $('sb-link').textContent = 'live';
    $('sb-link').classList.remove('down');
  };
  source.onerror = () => {
    if (!$('sb-link').classList.contains('down')) Logs.add('warn', 'event stream dropped; reconnecting');
    $('sb-link').textContent = 'reconnecting';
    $('sb-link').classList.add('down');
    // EventSource reconnects on its own, but only for transport hiccups. A
    // server that has gone away needs a deliberate retry with a backoff.
    if (source.readyState === EventSource.CLOSED) {
      setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 1.8, 12000);
    }
  };
}

function setState(next) {
  document.body.dataset.state = next || 'idle';
  const labels = {
    idle: 'Ready', listening: 'Listening', thinking: 'Thinking',
    speaking: 'Speaking', working: 'Working',
  };
  const label = labels[next] || next || 'Ready';
  $('sb-state').textContent = '';
  $('sb-state').append(el('i', 'sb-dot'), document.createTextNode(' ' + label));
  state.busy = !!next && next !== 'idle';
  $('btn-stop').hidden = !state.busy;
  Term.state(next);
}

/* =============================================================================
   Instruments
   ============================================================================= */
const Gauges = {
  update(data) {
    const set = (key, percent, label) => {
      const row = document.querySelector('.gauge[data-key="' + key + '"]');
      if (!row) return;
      const known = percent != null && !Number.isNaN(percent);
      row.querySelector('i').style.width = known ? clamp(percent, 0, 100) + '%' : '0%';
      row.querySelector('b').textContent = known ? (label == null ? Math.round(percent) + '%' : label) : '-';
      row.dataset.level = (known && percent >= 85) ? 'high' : 'normal';
    };
    set('cpu', data.cpu);
    set('ram', data.ram, data.ram_used_gb != null ? data.ram_used_gb.toFixed(1) + 'G' : null);
    set('disk', data.disk);
    set('battery', data.battery,
        data.battery != null ? Math.round(data.battery) + '%' + (data.plugged ? '\u26A1' : '') : null);

    const cores = $('cores');
    const values = data.cores || [];
    if (values.length && cores.children.length !== values.length) {
      cores.innerHTML = '';
      values.forEach(() => { const c = el('div', 'core'); c.appendChild(el('i')); cores.appendChild(c); });
    }
    values.forEach((value, i) => {
      const cell = cores.children[i];
      if (cell && cell.firstChild) cell.firstChild.style.height = clamp(value, 0, 100) + '%';
    });

    if (data.cpu != null) {
      $('sb-cpu').textContent = 'cpu ' + Math.round(data.cpu) + '%' +
        (data.ram != null ? ' · mem ' + Math.round(data.ram) + '%' : '');
    }
    facts('machine-facts', [
      ['Platform', data.platform],
      ['Processes', data.processes],
      ['Uptime', data.uptime ? Math.floor(data.uptime / 3600) + 'h' : null],
      ['Memory', data.ram_total_gb ? data.ram_used_gb + ' / ' + data.ram_total_gb + ' GB' : null],
      ['Workspace', BOOT.workspaceName || null],
    ]);
  },
};

/* =============================================================================
   Questions J.A.R.V.I.S. asks back
   ============================================================================= */
const Ask = {
  cards: new Map(),
  promptToken: null,

  card(data) {
    const node = el('div', 'ask' + (data.reversible === false ? ' danger' : ''));
    const kinds = {
      permission: 'Permission required', confirm: 'Confirm',
      choose: 'Choose', prompt: 'Your turn',
    };
    node.append(el('h4', '', kinds[data.ask] || 'Question'));
    node.append(el('p', '', data.question || data.prompt || 'J.A.R.V.I.S. needs an answer.'));
    if (data.target || data.detail) node.append(el('div', 'detail', data.detail || data.target));

    const actions = el('div', 'ask-actions');
    const answer = (value) => {
      api('/api/answer', { token: data.token, answer: value }).catch(() => {});
      this.close(data.token);
    };
    if (data.ask === 'permission') {
      const yes = el('button', 'btn', 'Allow once'); yes.onclick = () => answer('y');
      const always = el('button', 'btn subtle', 'Always allow'); always.onclick = () => answer('a');
      const no = el('button', 'btn subtle', 'Deny'); no.onclick = () => answer('n');
      actions.append(yes, always, no);
    } else if (data.ask === 'choose') {
      (data.options || []).forEach((option, i) => {
        const button = el('button', i ? 'btn subtle' : 'btn', option);
        button.onclick = () => answer(option);
        actions.appendChild(button);
      });
    } else if (data.ask === 'confirm') {
      const yes = el('button', 'btn', 'Yes'); yes.onclick = () => answer('y');
      const no = el('button', 'btn subtle', 'No'); no.onclick = () => answer('n');
      actions.append(yes, no);
    } else {
      // A prompt is answered from the composer, which is where the operator's
      // hands already are.
      node.append(el('div', 'detail', 'Type your answer below and press Enter.'));
      this.promptToken = data.token;
      $('input').focus();
    }
    if (actions.children.length) node.appendChild(actions);
    this.cards.set(data.token, node);
    Transcript.append(node);
    toast(kinds[data.ask] || 'J.A.R.V.I.S. asked you something');
  },

  close(token) {
    const node = this.cards.get(token);
    if (node) { node.classList.add('answered'); this.cards.delete(token); }
    if (this.promptToken === token) this.promptToken = null;
  },
};

/* =============================================================================
   Composer
   ============================================================================= */
const COMMANDS = [
  ['/clear', 'Wipe the conversation and the transcript'],
  ['/tools', 'List the instruments available'],
  ['/protocols', 'List the registered protocols'],
  ['/protocol ', 'Run a protocol by name'],
  ['/model', 'Report the model and its availability'],
  ['/metrics', 'Latency of the last turn'],
  ['/diag', 'Full diagnostics sweep'],
  ['/theme', 'Open the colour studio'],
  ['/quit', 'Shut J.A.R.V.I.S. down'],
];

const Composer = {
  input: null,
  slashIndex: 0,

  init() {
    this.input = $('input');
    this.input.addEventListener('input', () => { this.autosize(); this.slash(); });
    this.input.addEventListener('keydown', (event) => this.keys(event));
    $('composer').addEventListener('submit', (event) => { event.preventDefault(); this.send(); });
    $('btn-stop').onclick = () => {
      api('/api/interrupt', {}).catch(() => {});
      toast('Interrupted.');
    };
    this.autosize();
  },

  autosize() {
    this.input.style.height = 'auto';
    this.input.style.height = Math.min(this.input.scrollHeight, 208) + 'px';
  },

  send(text) {
    const value = String(text == null ? this.input.value : text).trim();
    if (!value) return;
    // A parked prompt() takes the line instead of the model.
    if (Ask.promptToken) {
      api('/api/answer', { token: Ask.promptToken, answer: value }).catch(() => {});
      Ask.close(Ask.promptToken);
    } else if (value === '/theme') {
      Studio.show();
      this.input.value = '';
      this.autosize();
      this.hideSlash();
      return;
    } else {
      api('/api/chat', { text: value })
        .catch((err) => Transcript.note('Could not send that: ' + err.message, 'error'));
    }
    this.input.value = '';
    this.autosize();
    this.hideSlash();
    this.input.focus();
  },

  keys(event) {
    const menu = $('slash-menu');
    if (!menu.hidden) {
      const items = Array.prototype.slice.call(menu.querySelectorAll('.slash-item'));
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        this.slashIndex = (this.slashIndex + step + items.length) % items.length;
        items.forEach((item, i) => item.classList.toggle('active', i === this.slashIndex));
        if (items[this.slashIndex]) items[this.slashIndex].scrollIntoView({ block: 'nearest' });
        return;
      }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
        event.preventDefault();
        if (items[this.slashIndex]) items[this.slashIndex].click();
        return;
      }
      if (event.key === 'Escape') { event.preventDefault(); this.hideSlash(); return; }
    }
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.send(); }
  },

  slash() {
    const value = this.input.value;
    if (!value.startsWith('/') || value.indexOf('\n') !== -1) return this.hideSlash();
    const matches = COMMANDS.filter((entry) => entry[0].startsWith(value.trim()));
    if (!matches.length) return this.hideSlash();
    const menu = $('slash-menu');
    menu.innerHTML = '';
    this.slashIndex = 0;
    matches.forEach((entry, i) => {
      const cmd = entry[0], what = entry[1];
      const item = el('button', 'slash-item' + (i ? '' : ' active'));
      item.type = 'button';
      item.append(el('span', 'cmd', cmd.trim()), el('span', 'what', what));
      item.onclick = () => {
        if (cmd.endsWith(' ')) { this.input.value = cmd; this.hideSlash(); this.input.focus(); }
        else this.send(cmd);
      };
      menu.appendChild(item);
    });
    menu.hidden = false;
  },

  hideSlash() { $('slash-menu').hidden = true; },
};

/* =============================================================================
   Command palette
   ============================================================================= */
const Palette = {
  items: [],
  index: 0,

  init() {
    this.items = COMMANDS.map((entry) => ({
      label: entry[0].trim(), what: entry[1], run: () => Composer.send(entry[0].trim()),
    })).concat([
      { label: 'Colours', what: 'Open the theme studio', run: () => Studio.show() },
      { label: 'Surprise me', what: 'A random theme that still looks good', run: () => Studio.push({ surprise: true }) },
      { label: 'Code', what: 'Show the code view', run: () => Rail.show('code') },
      { label: 'Terminal', what: 'Show the agentic terminal', run: () => Rail.show('terminal') },
      { label: 'System', what: 'Show instruments and machine gauges', run: () => Rail.show('system') },
      { label: 'Preview rail', what: 'Show or hide the right-hand rail', run: () => Shell.toggle('rail') },
      { label: 'Sidebar', what: 'Show or hide the left sidebar', run: () => Shell.toggle('sidebar') },
    ], (BOOT.protocols || []).map((name) => ({
      label: name, what: 'Run this protocol', run: () => Composer.send('/protocol ' + name),
    })));

    $('palette-input').addEventListener('input', () => this.render());
    $('palette-input').addEventListener('keydown', (event) => this.keys(event));
    $('palette-wrap').addEventListener('click', (event) => {
      if (event.target.id === 'palette-wrap') this.hide();
    });
  },

  show() {
    $('palette-wrap').hidden = false;
    $('palette-input').value = '';
    this.index = 0;
    this.render();
    $('palette-input').focus();
  },

  hide() { $('palette-wrap').hidden = true; Composer.input.focus(); },

  matches() {
    const query = $('palette-input').value.trim().toLowerCase();
    if (!query) return this.items;
    return this.items.filter((item) => (item.label + ' ' + item.what).toLowerCase().includes(query));
  },

  render() {
    const list = $('palette-list');
    list.innerHTML = '';
    const found = this.matches();
    this.index = clamp(this.index, 0, Math.max(0, found.length - 1));
    found.forEach((item, i) => {
      const row = el('li', i === this.index ? 'active' : '');
      row.append(el('span', 'cmd', item.label), el('span', 'what', item.what));
      row.onclick = () => { this.hide(); item.run(); };
      list.appendChild(row);
    });
    if (!found.length) list.appendChild(el('li', '', 'Nothing matches that.'));
  },

  keys(event) {
    const found = this.matches();
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      this.index = (this.index + step + found.length) % Math.max(found.length, 1);
      this.render();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const item = found[this.index];
      this.hide();
      if (item) item.run();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      this.hide();
    }
  },
};

/* =============================================================================
   The shell

   Three panes, two draggable seams, an icon strip down the outer edge. Widths
   live in CSS custom properties on :root, so a drag is one property write per
   frame and the grid does the rest — no layout maths here, nothing to sync.
   ============================================================================= */
const Shell = {
  MIN: { sidebar: 190, rail: 300 },
  MAX_FRACTION: 0.5,

  init() {
    this.node = $('shell');
    this.restore();
    this.seam('seam-left', 'sidebar', 1);
    this.seam('seam-right', 'rail', -1);
    // Transitions go on after the first paint, so a restored layout appears at
    // its remembered width rather than sliding into it.
    requestAnimationFrame(() => this.node.classList.add('animate'));
  },

  read(key, fallback) {
    try {
      const saved = Number(localStorage.getItem('jarvis.' + key));
      return Number.isFinite(saved) && saved > 0 ? saved : fallback;
    } catch (err) { return fallback; }
  },

  write(key, value) {
    try { localStorage.setItem('jarvis.' + key, String(value)); } catch (err) { /* private mode */ }
  },

  restore() {
    const root = document.documentElement;
    root.style.setProperty('--sidebar-width', this.read('sidebar', 244) + 'px');
    root.style.setProperty('--rail-width', this.read('rail', 400) + 'px');
    const wide = window.innerWidth;
    this.set('sidebar', this.read('sidebar-open', wide > 1180 ? 1 : 0) === 1);
    this.set('rail', this.read('rail-open', wide > 1180 ? 1 : 0) === 1);
  },

  set(which, open) {
    this.node.classList.toggle('no-' + which, !open);
    if (which === 'sidebar') $('btn-sidebar').classList.toggle('on', open);
    else $('btn-rail').classList.toggle('on', open);
    this.write(which + '-open', open ? 1 : 0);
  },

  toggle(which) { this.set(which, this.node.classList.contains('no-' + which)); },
  open(which) { if (this.node.classList.contains('no-' + which)) this.set(which, true); },
  isOpen(which) { return !this.node.classList.contains('no-' + which); },

  /* `direction` is +1 when the pane grows rightward (the sidebar), -1 when it
     grows leftward (the rail). Width is measured from the window edge rather
     than a stored start value, which stops a drag drifting when the pointer
     leaves and re-enters. */
  seam(id, which, direction) {
    const handle = $(id);
    const variable = '--' + which + '-width';
    let dragging = false;

    const apply = (clientX) => {
      const edge = which === 'rail' ? EDGE_WIDTH : 0;
      const raw = direction > 0 ? clientX : window.innerWidth - clientX - edge;
      const width = clamp(raw, this.MIN[which], window.innerWidth * this.MAX_FRACTION);
      document.documentElement.style.setProperty(variable, Math.round(width) + 'px');
    };

    handle.addEventListener('pointerdown', (event) => {
      dragging = true;
      handle.classList.add('active');
      document.body.classList.add('dragging');
      this.node.classList.remove('animate');   // or the pane lags the pointer
      handle.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    handle.addEventListener('pointermove', (event) => { if (dragging) apply(event.clientX); });

    const release = () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('active');
      document.body.classList.remove('dragging');
      this.node.classList.add('animate');
      const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue(variable), 10);
      if (Number.isFinite(current)) this.write(which, current);
    };
    handle.addEventListener('pointerup', release);
    handle.addEventListener('pointercancel', release);

    // Keyboard resize: a drag handle nobody can reach is not a control.
    handle.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const step = (event.shiftKey ? 40 : 12) * (event.key === 'ArrowRight' ? 1 : -1) * direction;
      const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue(variable), 10) || 0;
      const width = clamp(current + step, this.MIN[which], window.innerWidth * this.MAX_FRACTION);
      document.documentElement.style.setProperty(variable, Math.round(width) + 'px');
      this.write(which, Math.round(width));
    });
  },
};

const EDGE_WIDTH = 38;

/* =============================================================================
   The right panel
   ============================================================================= */
const VIEWS = ['code', 'terminal', 'agent', 'logs', 'system'];

const Rail = {
  view: 'terminal',

  init() {
    document.querySelectorAll('.rail-tab, .edge-btn').forEach((button) => {
      button.onclick = () => {
        // Clicking the strip icon for the view already showing closes the rail,
        // which is how every editor's activity bar behaves.
        if (button.classList.contains('edge-btn') && this.view === button.dataset.view && Shell.isOpen('rail')) {
          Shell.set('rail', false);
          return;
        }
        this.show(button.dataset.view);
      };
    });
    this.show(this.remembered());
  },

  remembered() {
    try {
      const saved = localStorage.getItem('jarvis.view');
      return VIEWS.indexOf(saved) === -1 ? 'terminal' : saved;
    } catch (err) { return 'terminal'; }
  },

  show(view) {
    if (VIEWS.indexOf(view) === -1) return;
    this.view = view;
    Shell.open('rail');
    try { localStorage.setItem('jarvis.view', view); } catch (err) { /* private mode */ }

    document.querySelectorAll('.rail-tab').forEach((tab) => {
      const on = tab.dataset.view === view;
      tab.classList.toggle('on', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.edge-btn').forEach((button) => {
      button.classList.toggle('on', button.dataset.view === view);
    });
    document.querySelectorAll('.rail-view').forEach((panel) => {
      panel.classList.toggle('on', panel.id === 'view-' + view);
    });
    this.unmark(view);
    if (view === 'terminal') Sh.focus();
    if (view === 'agent') Term.toBottom(true);
    if (view === 'logs') Logs.toBottom(true);
  },

  /* A dot on a tab that has something new while you are looking elsewhere. */
  mark(view) {
    if (this.view === view && Shell.isOpen('rail')) return;
    document.querySelectorAll('[data-view="' + view + '"]').forEach((node) => {
      if (!node.querySelector('.pip')) node.appendChild(el('span', 'pip'));
    });
  },

  unmark(view) {
    document.querySelectorAll('[data-view="' + view + '"] .pip').forEach((pip) => pip.remove());
  },
};

/* =============================================================================
   Logs

   The three columns every log viewer has, because they are the three you
   actually scan by: when, how bad, and what. Fed from the same event stream as
   everything else, plus the instrument calls, which is what makes it useful —
   a log that only carries system lines tells you nothing about the turn.
   ============================================================================= */
/* =============================================================================
   The system shell

   PowerShell on Windows, the login shell everywhere else, running as one
   long-lived process on the server so `cd` sticks and variables persist. This
   half is only a view: it echoes what you typed against the prompt, appends the
   lines the server streams back, and draws a new prompt when the exit status
   arrives. Command history on Up/Down, because a terminal without it is a toy.

   It is line-oriented, not a terminal emulator — there is no PTY behind it, so
   a full-screen program like `vim` or `top` has nothing to draw into. Ordinary
   commands, which is what a pane beside a conversation is actually for, work.
   ============================================================================= */
const Sh = {
  history: [],
  at: 0,
  busy: false,
  ready: false,
  info: {},

  init() {
    this.pane = $('shell-pane');
    this.scroll = $('shell-scroll');
    this.out = $('shell-out');
    this.input = $('shell-input');
    this.info = BOOT.shell || {};

    if (this.info.disabled) return this.unavailable('The shell pane is switched off in your configuration.');
    if (!this.info.available) return this.unavailable('No shell was found on this machine.');

    this.setPrompt(this.info.cwd || '');
    $('shell-status').textContent = this.info.name + ' · not started';

    // Clicking anywhere in the pane puts the cursor on the prompt, the way a
    // terminal does. Not when the operator is selecting output to copy.
    this.pane.addEventListener('mousedown', (event) => {
      if (event.target === this.input) return;
      if (String(window.getSelection())) return;
      setTimeout(() => this.focus(), 0);
    });
    this.input.addEventListener('keydown', (event) => this.keys(event));
    $('btn-shell-clear').onclick = () => { this.out.innerHTML = ''; this.focus(); };
    $('btn-shell-stop').onclick = () => this.interrupt();
    $('btn-shell-restart').onclick = () => {
      this.line('meta', '— restarting the shell —');
      api('/api/shell', { restart: true }).then((r) => this.adopt(r)).catch(() => {});
    };
  },

  unavailable(why) {
    $('shell-row').hidden = true;
    this.out.appendChild(el('div', 'shell-unavailable', why));
    $('shell-status').textContent = 'unavailable';
  },

  focus() { if (this.input && !$('shell-row').hidden) this.input.focus({ preventScroll: true }); },

  /* A full path eats the width a terminal needs for the command. Shorten it
     the way a shell prompt does: home becomes ~, and anything still long keeps
     its last two segments behind an ellipsis. */
  short(path) {
    let text = String(path || '');
    const home = BOOT.home || '';
    if (home && text.indexOf(home) === 0) text = '~' + text.slice(home.length);
    if (text.length <= 34) return text;
    const sep = text.indexOf('\\') !== -1 ? '\\' : '/';
    const parts = text.split(sep).filter(Boolean);
    return parts.length <= 2 ? text : '…' + sep + parts.slice(-2).join(sep);
  },

  setPrompt(cwd) {
    this.cwd = cwd || '';
    const shape = this.info.prompt || '{cwd} $ ';
    const line = shape.replace('{cwd}', this.short(this.cwd)).trimEnd();
    $('shell-prompt').textContent = line;
    $('shell-prompt').title = this.cwd;
  },

  adopt(result) {
    if (!result) return;
    if (result.cwd) this.setPrompt(result.cwd);
    if (result.name) this.info.name = result.name;
    this.setBusy(false);
    $('shell-status').textContent = this.info.name + ' · ready';
  },

  line(kind, text) {
    const row = el('div', 'sline ' + (kind || ''));
    row.textContent = text;
    this.out.appendChild(row);
    this.trim();
    this.toBottom();
    return row;
  },

  /* An unbounded scrollback is a memory leak with a cursor in it. */
  trim() {
    const excess = this.out.children.length - 2000;
    for (let i = 0; i < excess; i++) this.out.removeChild(this.out.firstChild);
  },

  toBottom() { this.scroll.scrollTop = this.scroll.scrollHeight; },

  setBusy(busy) {
    this.busy = busy;
    this.pane.classList.toggle('busy', busy);
    this.input.disabled = false;   // typing ahead is allowed; a terminal allows it
  },

  run(text) {
    const command = String(text == null ? this.input.value : text);
    // The echo is drawn here rather than by the shell: without a PTY the shell
    // never echoes, and a terminal that does not show what you typed is useless.
    const row = this.line('echo', '');
    row.append(el('span', 'p', $('shell-prompt').textContent + ' '), document.createTextNode(command));

    this.input.value = '';
    if (command.trim()) {
      this.history.push(command);
      if (this.history.length > 400) this.history.shift();
    }
    this.at = this.history.length;
    this.setBusy(true);
    this.toBottom();
    $('shell-status').textContent = this.info.name + ' · running';
    api('/api/shell', { input: command }).then((result) => {
      if (result && result.ok === false) {
        this.line('fail', result.error || 'the shell refused that');
        this.setBusy(false);
      }
    }).catch((err) => {
      this.line('fail', 'could not reach the shell: ' + err.message);
      this.setBusy(false);
    });
  },

  interrupt() {
    api('/api/shell', { interrupt: true }).catch(() => {});
    this.line('meta', '^C');
    this.setBusy(false);
  },

  keys(event) {
    if (event.key === 'Enter') { event.preventDefault(); this.run(); return; }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (!this.history.length) return;
      this.at = Math.max(0, this.at - 1);
      this.input.value = this.history[this.at] || '';
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (!this.history.length) return;
      this.at = Math.min(this.history.length, this.at + 1);
      this.input.value = this.at === this.history.length ? '' : this.history[this.at];
      return;
    }
    if (event.key.toLowerCase() === 'c' && event.ctrlKey && !String(window.getSelection())) {
      event.preventDefault(); this.interrupt(); return;
    }
    if (event.key.toLowerCase() === 'l' && event.ctrlKey) {
      event.preventDefault(); this.out.innerHTML = ''; return;
    }
    // Esc belongs to the shell while the cursor is in it, not to the app.
    if (event.key === 'Escape') { event.stopPropagation(); this.input.value = ''; }
  },

  // -- events from the server -----------------------------------------------
  onOutput(text) { this.line('', text); Rail.mark('terminal'); },

  onDone(code, cwd) {
    this.setPrompt(cwd);
    if (code) this.line('exit', '').append(document.createTextNode('exit '), el('b', '', String(code)));
    this.setBusy(false);
    this.ready = true;
    $('shell-status').textContent = this.info.name + ' · ' + (code ? 'exit ' + code : 'ready');
  },

  onExit(code) {
    this.line('meta', '— the shell exited' + (code == null ? '' : ' (' + code + ')') + '; Restart to start a new one —');
    this.setBusy(false);
    $('shell-status').textContent = this.info.name + ' · stopped';
  },
};

const LEVELS = ['all', 'info', 'tool', 'warn', 'error'];

const Logs = {
  lines: [],
  filter: 'all',
  atBottom: true,
  LIMIT: 1000,

  init() {
    this.node = $('logs');
    this.node.addEventListener('scroll', () => {
      const gap = this.node.scrollHeight - this.node.scrollTop - this.node.clientHeight;
      this.atBottom = gap < 40;
    }, { passive: true });

    const bar = $('log-filters');
    LEVELS.forEach((level) => {
      const button = el('button', 'log-filter' + (level === 'all' ? ' on' : ''));
      button.dataset.level = level;
      button.append(document.createTextNode(level), el('span', 'n', '0'));
      button.onclick = () => this.setFilter(level);
      bar.appendChild(button);
    });
    $('btn-log-clear').onclick = () => { this.lines = []; this.render(); };
    this.add('info', 'Desktop front end attached to the running session.');
  },

  add(level, message) {
    const now = new Date();
    this.lines.push({
      level: level,
      message: String(message == null ? '' : message),
      at: now.toTimeString().slice(0, 8),
    });
    if (this.lines.length > this.LIMIT) this.lines.splice(0, this.lines.length - this.LIMIT);
    this.render();
    if (level === 'error' || level === 'warn') Rail.mark('logs');
  },

  setFilter(level) {
    this.filter = level;
    document.querySelectorAll('.log-filter').forEach((button) => {
      button.classList.toggle('on', button.dataset.level === level);
    });
    this.render();
    this.toBottom(true);
  },

  counts() {
    const tally = { all: this.lines.length, info: 0, tool: 0, warn: 0, error: 0 };
    this.lines.forEach((line) => {
      if (tally[line.level] === undefined) tally.info += 1;
      else tally[line.level] += 1;
    });
    return tally;
  },

  render() {
    const tally = this.counts();
    document.querySelectorAll('.log-filter').forEach((button) => {
      button.querySelector('.n').textContent = String(tally[button.dataset.level] || 0);
    });

    const shown = this.filter === 'all'
      ? this.lines
      : this.lines.filter((line) => line.level === this.filter);

    const batch = document.createDocumentFragment();
    shown.forEach((line) => {
      const row = el('div', 'log-line');
      row.dataset.level = line.level;
      row.append(
        el('span', 'at', line.at),
        el('span', 'lv', line.level),
        el('span', 'msg', line.message),
      );
      batch.appendChild(row);
    });
    this.node.innerHTML = '';
    this.node.appendChild(batch);
    $('log-count').textContent = shown.length + (shown.length === 1 ? ' line' : ' lines');
    this.toBottom();
  },

  toBottom(force) {
    if (!force && !this.atBottom) return;
    this.node.scrollTop = this.node.scrollHeight;
  },
};
/* =============================================================================
   The workspace tree

   Lazily loaded: a directory is fetched the first time it is opened, and the
   server only ever answers for paths inside the workspace.
   ============================================================================= */
const FOLDER_ICON = 'M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z';
const FILE_ICON = 'M6 3h7l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM13 3v5h5';

const Tree = {
  open: new Set(),

  init() {
    $('btn-tree-refresh').onclick = () => this.reload();
    this.reload();
  },

  reload() {
    this.known = new Map();
    $('tree').innerHTML = '';
    this.load('', $('tree'));
  },

  async load(path, host) {
    let data;
    try {
      data = await api('/api/files?path=' + encodeURIComponent(path));
    } catch (err) {
      host.appendChild(el('div', 'rail-note', 'The workspace could not be read.'));
      return;
    }
    if (!data || data.error) {
      host.appendChild(el('div', 'rail-note', (data && data.error) || 'Nothing there.'));
      return;
    }
    (data.entries || []).forEach((entry) => host.appendChild(this.node(entry)));
    if (data.truncated) host.appendChild(el('div', 'rail-note', 'Listing truncated.'));
  },

  node(entry) {
    const wrap = el('div');
    const row = el('button', 'row');
    row.type = 'button';
    row.setAttribute('role', 'treeitem');
    row.title = entry.path;

    if (entry.kind === 'dir') {
      const twist = icon('M9 6l6 6-6 6', 'twist');
      row.append(twist, icon(FOLDER_ICON, 'ficon'), el('span', 'fname', entry.name));
    } else {
      row.append(el('span', 'twist'), icon(FILE_ICON, 'ficon'), el('span', 'fname', entry.name));
      if (entry.size) row.append(el('span', 'fsize', bytes(entry.size)));
    }
    wrap.appendChild(row);

    if (entry.kind === 'dir') {
      const children = el('div', 'tree-children');
      children.hidden = true;
      wrap.appendChild(children);
      row.onclick = () => {
        const opening = children.hidden;
        children.hidden = !opening;
        row.classList.toggle('open', opening);
        if (opening && !children.dataset.loaded) {
          children.dataset.loaded = '1';
          this.load(entry.path, children);
        }
      };
    } else {
      row.onclick = () => {
        document.querySelectorAll('.tree .row.on').forEach((r) => r.classList.remove('on'));
        row.classList.add('on');
        Code.open(entry.path);
      };
    }
    return wrap;
  },
};

function icon(path, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', cls);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  shape.setAttribute('d', path);
  svg.appendChild(shape);
  return svg;
}

function bytes(n) {
  if (n < 1024) return n + 'B';
  if (n < 1024 * 1024) return Math.round(n / 1024) + 'K';
  return (n / 1024 / 1024).toFixed(1) + 'M';
}

/* =============================================================================
   Syntax highlighting

   Small and deliberately generic: one tokeniser, a per-language keyword list,
   and the theme's own colours. It is not a parser and does not pretend to be —
   it is here so a file is pleasant to read, and it degrades to plain text on
   anything it does not recognise rather than mangling it.

   It runs on escaped text and emits only spans with fixed class names, so a
   file full of angle brackets is coloured, not executed.
   ============================================================================= */
const KEYWORDS = {
  python: 'False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case self cls',
  javascript: 'async await break case catch class const continue debugger default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while with yield true false null undefined',
  typescript: 'abstract any as async await boolean break case catch class const continue declare default delete do else enum export extends finally for from function if implements import in instanceof interface let namespace new number of private protected public readonly return static string super switch this throw try type typeof var void while yield true false null undefined',
  rust: 'as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while',
  go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil',
  c: 'auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while',
  java: 'abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static super switch synchronized this throw throws transient try void volatile while true false null',
  bash: 'if then else elif fi for while do done case esac function return local export readonly declare source alias unset shift exit trap set',
  sql: 'select from where insert into update delete create table drop alter add join left right inner outer on group by order having limit offset union all as and or not null distinct values set index primary key foreign references',
  css: 'important media supports keyframes import charset font-face root and not or from to',
  yaml: 'true false null yes no on off',
  json: 'true false null',
  html: '',
  markdown: '',
  text: '',
};
KEYWORDS.cpp = KEYWORDS.c + ' bool catch class constexpr delete explicit friend namespace new nullptr operator private protected public template this throw try typename using virtual';
KEYWORDS.kotlin = KEYWORDS.java;
KEYWORDS.ruby = 'def end class module if elsif else unless while until for in do return yield begin rescue ensure raise nil true false self require attr_accessor';
KEYWORDS.php = KEYWORDS.c + ' echo function foreach as namespace use public private protected class extends implements new $this';
KEYWORDS.toml = KEYWORDS.ini = KEYWORDS.yaml;
KEYWORDS.swift = KEYWORDS.java;

/* Line comment openers by language, so a comment is a comment and not a
   division sign. */
const LINE_COMMENT = {
  python: '#', bash: '#', yaml: '#', toml: '#', ini: '#', r: '#', ruby: '#',
  javascript: '//', typescript: '//', rust: '//', go: '//', c: '//', cpp: '//',
  java: '//', kotlin: '//', swift: '//', php: '//', dart: '//', scala: '//',
  sql: '--', lua: '--', haskell: '--',
};

/* Openers that run past the end of a line. Without these a Python docstring is
   tokenised as code for its whole length, which is the single most obvious way
   a highlighter can look broken. `state` is threaded down the file by the
   caller and carries which one, if any, is still open. */
const BLOCK_MARKS = {
  python: ['\u0022\u0022\u0022', "'''"],
  javascript: ['/*'], typescript: ['/*'], c: ['/*'], cpp: ['/*'], java: ['/*'],
  css: ['/*'], rust: ['/*'], go: ['/*'], php: ['/*'], swift: ['/*'], kotlin: ['/*'],
  html: ['<!--'], xml: ['<!--'], markdown: [],
};
const BLOCK_CLOSE = { '<!--': '-->', '/*': '*/' };

function highlight(line, language, state) {
  const escaped = esc(line);
  if (!language || language === 'text') return escaped;

  const words = KEYWORDS[language];
  const keywords = words ? new Set(words.split(' ')) : null;
  const comment = LINE_COMMENT[language];
  const marks = BLOCK_MARKS[language] || [];

  // Already inside a block that opened on an earlier line: colour up to its
  // close and hand the rest back to the normal pass.
  if (state && state.open) {
    const closer = esc(BLOCK_CLOSE[state.open] || state.open);
    const at = escaped.indexOf(closer);
    const cls = state.open === '/*' || state.open === '<!--' ? 'tok-com' : 'tok-str';
    if (at === -1) return '<span class="' + cls + '">' + (escaped || ' ') + '</span>';
    state.open = null;
    const head = escaped.slice(0, at + closer.length);
    return '<span class="' + cls + '">' + head + '</span>' +
           highlight(unesc(escaped.slice(at + closer.length)), language, state);
  }

  // One pass, left to right. Strings and comments swallow everything inside
  // them, which is the only way a single regex sweep gets those two right.
  let out = '';
  let i = 0;
  const n = escaped.length;

  while (i < n) {
    const rest = escaped.slice(i);

    // Entities first, always. esc() has already turned & < > " ' into entities,
    // and any rule that consumes one of those bytes on its own splits the
    // entity in half — which reaches the screen as a literal `&quot;`.
    const entity = rest.match(/^&(?:[a-z]+|#\d+);/);
    if (entity && !rest.startsWith('&quot;') && !rest.startsWith('&#39;')) {
      out += entity[0];
      i += entity[0].length;
      continue;
    }

    // A block that opens and does not close on this line takes the remainder
    // and is remembered for the next one.
    let opened = null;
    for (let m = 0; m < marks.length; m++) {
      if (rest.startsWith(esc(marks[m]))) { opened = marks[m]; break; }
    }
    if (opened) {
      const closer = esc(BLOCK_CLOSE[opened] || opened);
      const from = esc(opened).length;
      const at = rest.indexOf(closer, from);
      const cls = opened === '/*' || opened === '<!--' ? 'tok-com' : 'tok-str';
      if (at === -1) {
        if (state) state.open = opened;
        out += '<span class="' + cls + '">' + rest + '</span>';
        break;
      }
      const chunk = rest.slice(0, at + closer.length);
      out += '<span class="' + cls + '">' + chunk + '</span>';
      i += chunk.length;
      continue;
    }

    if (comment && rest.startsWith(esc(comment))) {
      out += '<span class="tok-com">' + rest + '</span>';
      break;
    }
    const quote = rest.match(/^(&quot;|&#39;|`)/);
    if (quote) {
      const mark = quote[1];
      let end = mark.length;
      while (end < rest.length) {
        if (rest.slice(end).startsWith('\\')) { end += 2; continue; }
        if (rest.slice(end).startsWith(mark)) { end += mark.length; break; }
        end += 1;
      }
      out += '<span class="tok-str">' + rest.slice(0, end) + '</span>';
      i += end;
      continue;
    }

    const number = rest.match(/^\b\d[\d_]*(\.\d+)?([eE][+-]?\d+)?\b|^0[xXbBoO][0-9a-fA-F_]+/);
    if (number) {
      out += '<span class="tok-num">' + number[0] + '</span>';
      i += number[0].length;
      continue;
    }

    const word = rest.match(/^[A-Za-z_$][A-Za-z0-9_$]*/);
    if (word) {
      const text = word[0];
      const after = rest.slice(text.length).match(/^\s*\(/);
      let cls = '';
      if (keywords && keywords.has(text)) cls = 'tok-key';
      else if (after) cls = 'tok-fn';
      else if (/^[A-Z][A-Za-z0-9_]*$/.test(text)) cls = 'tok-type';
      out += cls ? '<span class="' + cls + '">' + text + '</span>' : text;
      i += text.length;
      continue;
    }

    const punctuation = rest.match(/^[{}()[\];:,.+\-*/%=!|^~?@]+/);
    if (punctuation) {
      out += '<span class="tok-punc">' + punctuation[0] + '</span>';
      i += punctuation[0].length;
      continue;
    }

    out += rest[0];
    i += 1;
  }
  return out;
}

/* =============================================================================
   The code section

   Open files live as tabs, each keeping its own scroll position. Rendering is
   row-per-line so the gutter can be sticky and unselectable — copying a file
   should give you the file, not the line numbers.
   ============================================================================= */
const Code = {
  files: new Map(),      // path -> {name, language, content, lines, scroll}
  current: null,

  init() {
    $('btn-code-copy').onclick = () => this.copy();
    $('btn-code-ask').onclick = () => this.ask();
    const wrap = $('btn-code-wrap');
    try { this.wrapped = localStorage.getItem('jarvis.wrap') === '1'; } catch (err) { this.wrapped = false; }
    this.applyWrap();
    wrap.onclick = () => {
      this.wrapped = !this.wrapped;
      try { localStorage.setItem('jarvis.wrap', this.wrapped ? '1' : '0'); } catch (err) { /* private mode */ }
      this.applyWrap();
    };
  },

  applyWrap() {
    $('code-host').classList.toggle('wrapped', this.wrapped);
    $('btn-code-wrap').classList.toggle('on', this.wrapped);
  },

  async open(path, options) {
    Rail.show('code');
    if (this.files.has(path)) return this.select(path);

    let data;
    try {
      data = await api('/api/file?path=' + encodeURIComponent(path));
    } catch (err) {
      toast('That file would not open.');
      return;
    }
    if (!data || data.error) { toast((data && data.error) || 'That file would not open.'); return; }
    if (data.binary) { toast(data.name + ' is a binary file.'); return; }

    this.files.set(path, {
      path: data.path, name: data.name, language: data.language,
      content: data.content, lines: data.lines, truncated: data.truncated,
      size: data.size, scroll: 0,
    });
    this.select(path);
    if (options && options.quiet !== true) Rail.mark('code');
  },

  select(path) {
    const file = this.files.get(path);
    if (!file) return;
    if (this.current && this.files.has(this.current)) {
      this.files.get(this.current).scroll = $('code-scroll').scrollTop;
    }
    this.current = path;
    this.renderTabs();
    this.renderCrumbs(file);
    this.renderBody(file);
    $('sb-file').textContent = file.name + ' · ' + file.lines + ' lines';
    $('artifact-count').textContent = String(this.files.size);
  },

  close(path) {
    this.files.delete(path);
    if (this.current === path) {
      this.current = null;
      const next = this.files.keys().next();
      if (next.done) this.blank(); else this.select(next.value);
    } else {
      this.renderTabs();
    }
  },

  blank() {
    $('artifact-count').textContent = String(this.files.size);
    $('code-empty').hidden = false;
    $('code-scroll').hidden = true;
    $('code-foot').hidden = true;
    $('crumbs').innerHTML = '';
    $('sb-file').textContent = '';
    this.renderTabs();
  },

  renderTabs() {
    const strip = $('tabstrip');
    strip.innerHTML = '';
    this.files.forEach((file, path) => {
      const tab = el('button', 'ftab' + (path === this.current ? ' on' : ''));
      tab.type = 'button';
      tab.title = path;
      tab.append(el('span', 'fname', file.name));
      const close = el('span', 'close', '×');
      close.onclick = (event) => { event.stopPropagation(); this.close(path); };
      tab.append(close);
      tab.onclick = () => this.select(path);
      strip.appendChild(tab);
    });
  },

  renderCrumbs(file) {
    const crumbs = $('crumbs');
    crumbs.innerHTML = '';
    const parts = String(file.path || file.name).split('/');
    crumbs.appendChild(el('span', '', BOOT.workspaceName || 'workspace'));
    parts.forEach((part, i) => {
      crumbs.appendChild(el('span', 'sep', '/'));
      crumbs.appendChild(el('span', i === parts.length - 1 ? 'leaf' : '', part));
    });
  },

  renderBody(file) {
    $('code-empty').hidden = true;
    $('code-scroll').hidden = false;
    $('code-foot').hidden = false;

    const body = $('code-body');
    body.innerHTML = '';
    const lines = file.content.split('\n');
    // A document fragment keeps a four-thousand-line file to one reflow.
    const batch = document.createDocumentFragment();
    const state = { open: null };   // carries an unclosed docstring down the file
    lines.forEach((line, i) => {
      const row = document.createElement('tr');
      const gutter = document.createElement('td');
      gutter.className = 'ln';
      gutter.textContent = String(i + 1);
      const src = document.createElement('td');
      src.className = 'src';
      src.innerHTML = highlight(line, file.language, state) || '&nbsp;';
      row.append(gutter, src);
      batch.appendChild(row);
    });
    body.appendChild(batch);

    $('code-meta').textContent =
      file.language + ' · ' + file.lines + ' lines · ' + bytes(file.size) +
      (file.truncated ? ' · truncated' : '');
    $('code-scroll').scrollTop = file.scroll || 0;
  },

  async copy() {
    const file = this.files.get(this.current);
    if (!file) return;
    try {
      await navigator.clipboard.writeText(file.content);
      toast('Copied ' + file.name + '.');
    } catch (err) { toast('The clipboard refused that.'); }
  },

  ask() {
    const file = this.files.get(this.current);
    if (!file) return;
    Composer.input.value = 'About `' + file.path + '`: ';
    Composer.input.focus();
    Composer.autosize();
  },
};

/* =============================================================================
   The agentic terminal, inside the app

   The full-screen HUD's grammar, rendered from the same event stream the chat
   pane reads: a bullet for anything J.A.R.V.I.S. did or said, its reading
   hanging underneath, a caret for what the operator typed. This does not
   replace the terminal front end — that still runs, on its own, in a real
   terminal. It reproduces it here so the window is not a lesser view of the
   same session.
   ============================================================================= */
const BULLET = '⏺';    // the filled circle that opens an action
const BRANCH = '⎿';    // the elbow that hangs a result under it
const CARET  = '›';    // what the operator typed

const Term = {
  atBottom: true,
  streaming: null,

  init() {
    this.node = $('term');
    this.node.addEventListener('scroll', () => {
      const gap = this.node.scrollHeight - this.node.scrollTop - this.node.clientHeight;
      this.atBottom = gap < 40;
    }, { passive: true });
    $('btn-term-clear').onclick = () => { this.node.innerHTML = ''; };
    this.line('system', BULLET, 'J.A.R.V.I.S. online. This is the same session as the terminal.');
  },

  line(kind, glyph, text) {
    const row = el('div', 'tline t-' + kind);
    row.append(el('span', 'glyph', glyph), el('span', 'txt', text));
    this.node.appendChild(row);
    this.trim();
    this.toBottom();
    Rail.mark('terminal');
    return row;
  },

  /* An unbounded terminal is a memory leak with a scrollbar. */
  trim() {
    const excess = this.node.children.length - 600;
    for (let i = 0; i < excess; i++) this.node.removeChild(this.node.firstChild);
  },

  toBottom(force) {
    if (!force && !this.atBottom) return;
    this.node.scrollTop = this.node.scrollHeight;
  },

  user(text) { this.line('user', CARET, text); },
  system(text, level) {
    this.line(level === 'error' ? 'error' : level === 'warn' ? 'warn' : 'system', BULLET, text);
  },
  thought(text) { this.line('think', BRANCH, text); },

  toolStart(name, args) {
    const detail = (args && Object.keys(args).length) ? ' ' + JSON.stringify(args) : '';
    this.line('tool', BULLET, name + '(' + detail.trim() + ')');
  },

  toolEnd(name, ok, summary) {
    this.line(ok ? 'result' : 'error', BRANCH, summary || (ok ? 'done' : 'failed'));
  },

  /* A terminal shows prose, not source. The full-screen HUD renders Markdown
     through Rich; here the markers are simply stripped, which gets the same
     readable result without a second renderer. Fenced blocks keep their body
     and lose their fences, indented so they still read as code. */
  flatten(text) {
    // Fenced code comes out first and goes back in untouched. Stripping
    // emphasis across a code block turns `a * b * c` into `a b c`, which is a
    // worse lie than leaving the fences in.
    const blocks = [];
    const prose = String(text || '').replace(/```[a-zA-Z0-9]*\n?([\s\S]*?)```/g, (_, code) => {
      blocks.push(code.replace(/\n$/, ''));
      return '\u0000' + (blocks.length - 1) + '\u0000';
    });
    return prose
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,;:!?])/g, '$1$2')
      .replace(/`([^`\n]+)`/g, '$1')
      .replace(/^\s*>\s?/gm, '  ')
      .replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)] || '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  },

  streamBegin() {
    this.streaming = this.line('agent', BULLET, '');
    this.buffer = '';
  },

  streamToken(token) {
    if (!this.streaming) this.streamBegin();
    this.buffer += token;
    const txt = this.streaming.querySelector('.txt');
    txt.textContent = this.buffer;
    txt.appendChild(el('span', 'term-cursor'));
    this.toBottom();
  },

  streamEnd(text) {
    if (!this.streaming) {
      if (text) this.line('agent', BULLET, this.flatten(text));
      return;
    }
    const final = this.flatten(text || this.buffer);
    this.streaming.querySelector('.txt').textContent = final;
    if (!final.trim()) this.streaming.remove();
    this.streaming = null;
    this.buffer = '';
    this.toBottom();
  },

  state(next) {
    $('term-title').textContent = 'jarvis · ' + (next || 'idle');
  },
};

/* =============================================================================
   Chrome
   ============================================================================= */
function toast(text) {
  const node = el('div', 'toast', text);
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 220);
  }, 2600);
}

/* =============================================================================
   Sessions

   One agent core, several surfaces: a "new session" clears the shared memory
   rather than forking a second assistant, which is exactly what /clear does in
   the terminal. The list is a record of this window's turns, grouped the way
   every chat client groups them — by when, not by index.
   ============================================================================= */
const Sessions = {
  items: [],
  pinned: [],
  query: '',

  init() {
    $('btn-new').onclick = () => this.create();
    $('btn-foot-new').onclick = () => this.create();
    $('btn-swap').onclick = () => Palette.show();
    $('session-search').addEventListener('input', (event) => {
      this.query = event.target.value.trim().toLowerCase();
      this.render();
    });
    this.items.push({ label: 'Current session', at: Date.now(), live: true });
    this.render();
  },

  /* The first thing the operator says becomes the session's name, the way a
     chat client titles a thread. Until then it is "Current session". */
  name(text) {
    const live = this.items.find((item) => item.live);
    if (!live || live.named) return;
    live.named = true;
    live.label = text.length > 42 ? text.slice(0, 41) + '…' : text;
    $('session-title').textContent = live.label;
    this.render();
  },

  create() {
    Composer.send('/clear');
    this.items.forEach((item) => { item.live = false; });
    this.items.unshift({ label: 'Current session', at: Date.now(), live: true });
    $('session-title').textContent = 'New session';
    this.render();
    Composer.input.focus();
  },

  select(item, event) {
    // Shift-click pins, exactly as the empty-state hint promises.
    if (event && event.shiftKey) {
      const at = this.pinned.indexOf(item);
      if (at === -1) this.pinned.unshift(item); else this.pinned.splice(at, 1);
      this.render();
      return;
    }
    toast('This window keeps one live session — the terminal and the app share it.');
  },

  /* Today / Yesterday / a month name. The grouping every chat client uses. */
  group(at) {
    const then = new Date(at);
    const now = new Date();
    const days = Math.floor((now.setHours(0, 0, 0, 0) - new Date(at).setHours(0, 0, 0, 0)) / 86400000);
    if (days <= 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days < 7) return 'This week';
    return then.toLocaleDateString([], { month: 'long' });
  },

  row(item) {
    const node = el('li', item.live ? 'on' : '');
    node.append(el('span', 'dot'));
    node.append(el('span', 'label', item.label));
    if (this.pinned.indexOf(item) !== -1) node.title = 'Pinned — shift-click to unpin';
    const stamp = el('time', '', new Date(item.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
    node.append(stamp);
    node.onclick = (event) => this.select(item, event);
    return node;
  },

  matches(item) {
    return !this.query || item.label.toLowerCase().indexOf(this.query) !== -1;
  },

  render() {
    const pinnedList = $('pinned-list');
    pinnedList.innerHTML = '';
    const pins = this.pinned.filter((item) => this.matches(item));
    pins.forEach((item) => pinnedList.appendChild(this.row(item)));
    $('pinned-empty').hidden = pins.length > 0;

    const list = $('session-list');
    list.innerHTML = '';
    const shown = this.items.filter((item) => this.matches(item));
    let group = null;
    shown.forEach((item) => {
      const label = this.group(item.at);
      if (label !== group) {
        group = label;
        list.appendChild(el('li', 'group', label));
      }
      list.appendChild(this.row(item));
    });
    $('session-count').textContent = String(this.items.length);
  },
};

/* =============================================================================
   The keyboard sheet
   ============================================================================= */
const KEYMAP = [
  ['Conversation', [
    ['Send', ['Enter']],
    ['Newline', ['Shift', 'Enter']],
    ['Command completion', ['/']],
    ['Interrupt the turn', ['Esc']],
    ['New session', ['Ctrl', 'N']],
  ]],
  ['Panels', [
    ['Command palette', ['Ctrl', 'K']],
    ['Appearance', ['Ctrl', '/']],
    ['Right panel', ['Ctrl', 'B']],
    ['Sidebar', ['Ctrl', '\\']],
    ['Code · Terminal · Agent · Logs · System', ['Ctrl', '1–5']],
    ['This sheet', ['?']],
  ]],
  ['Code', [
    ['Close the open file', ['Ctrl', 'W']],
  ]],
  ['Terminal', [
    ['Run', ['Enter']],
    ['History', ['\u2191', '\u2193']],
    ['Interrupt', ['Ctrl', 'C']],
    ['Clear the scrollback', ['Ctrl', 'L']],
  ]],
];

const Keys = {
  init() {
    const sheet = $('keymap');
    KEYMAP.forEach((section) => {
      sheet.appendChild(el('div', 'sect', section[0]));
      section[1].forEach((entry) => {
        sheet.appendChild(el('dt', '', entry[0]));
        const keys = el('dd');
        entry[1].forEach((key) => keys.appendChild(el('kbd', 'hint', key)));
        sheet.appendChild(keys);
      });
    });
    $('keys-wrap').addEventListener('click', (event) => {
      if (event.target.id === 'keys-wrap') this.hide();
    });
    $('btn-keys').onclick = () => this.show();
  },
  show() { $('keys-wrap').hidden = false; },
  hide() { $('keys-wrap').hidden = true; },
  get open() { return !$('keys-wrap').hidden; },
};

/* =============================================================================
   Boot
   ============================================================================= */
const SUGGESTIONS = [
  'What is this machine doing right now?',
  'Open jarvis/theme.py and walk me through it',
  'Run a diagnostics sweep',
];

function facts(host, rows) {
  const node = $(host);
  node.innerHTML = '';
  rows.forEach((row) => {
    if (row[1] === null || row[1] === undefined || row[1] === '') return;
    node.append(el('dt', '', row[0]), el('dd', '', String(row[1])));
  });
}

function boot() {
  $('chip-model').textContent = BOOT.model || 'no model';
  $('sb-model').textContent = BOOT.model || '—';
  $('sb-cwd').textContent = BOOT.workspace || '';
  $('cwd-label').textContent = BOOT.workspace || '~';
  document.title = (BOOT.agent || 'J.A.R.V.I.S.') + ' Desktop';

  const tools = BOOT.tools || [];
  const protocols = BOOT.protocols || [];
  $('tool-count').textContent = String(tools.length);
  $('cap-count').textContent = String(tools.length + protocols.length);
  tools.forEach((name) => $('tool-list').appendChild(el('li', '', name)));
  protocols.forEach((name) => {
    const chip = el('li', 'action', name);
    chip.title = 'Run the ' + name + ' protocol';
    chip.onclick = () => Composer.send('/protocol ' + name);
    $('protocol-list').appendChild(chip);
  });

  facts('model-facts', [
    ['Model', BOOT.model || '—'],
    ['Host', BOOT.host || '—'],
    ['Mode', BOOT.standalone ? 'standalone' : 'attached to a terminal'],
    ['Turn', 'not measured yet'],
  ]);
  facts('machine-facts', [['Workspace', BOOT.workspaceName || '—']]);

  SUGGESTIONS.forEach((text) => {
    const button = el('button', 'suggestion', text);
    button.onclick = () => Composer.send(text);
    $('suggestions').appendChild(button);
  });

  applyVariables(BOOT.variables);
  Shell.init();
  Transcript.init();
  Composer.init();
  Palette.init();
  Studio.init();
  Code.init();
  Term.init();
  Logs.init();
  Sh.init();
  Rail.init();
  Tree.init();
  Sessions.init();
  Keys.init();
  setState(BOOT.state || 'idle');

  $('btn-palette').onclick = () => Studio.toggle();
  $('btn-command').onclick = () => Palette.show();
  $('btn-rail').onclick = () => Shell.toggle('rail');
  $('btn-sidebar').onclick = () => Shell.toggle('sidebar');
  $('scrim').onclick = () => Studio.hide();
  $('btn-attach').onclick = () => { Rail.show('code'); Shell.open('sidebar'); };
  $('model-chip').onclick = () => { Rail.show('system'); };
  $('btn-mic').onclick = () => Composer.send('talk');
  $('btn-mute').onclick = () => Composer.send('quiet');
  $('btn-mute-all').onclick = () => Composer.send('quiet');
  document.querySelectorAll('.nav-item[data-rail]').forEach((item) => {
    item.onclick = () => Rail.show(item.dataset.rail);
  });

  document.addEventListener('keydown', (event) => {
    const meta = event.ctrlKey || event.metaKey;
    const typing = !!event.target.closest('input, textarea');

    if (meta && event.key.toLowerCase() === 'k') { event.preventDefault(); Palette.show(); }
    else if (meta && event.key === '/') { event.preventDefault(); Studio.toggle(); }
    else if (meta && event.key.toLowerCase() === 'b') { event.preventDefault(); Shell.toggle('rail'); }
    else if (meta && event.key === '\\') { event.preventDefault(); Shell.toggle('sidebar'); }
    else if (meta && event.key.toLowerCase() === 'n') { event.preventDefault(); Sessions.create(); }
    else if (meta && event.key >= '1' && event.key <= '5') {
      event.preventDefault();
      Rail.show(VIEWS[Number(event.key) - 1]);
    } else if (meta && event.key.toLowerCase() === 'w' && Rail.view === 'code' && Code.current) {
      event.preventDefault(); Code.close(Code.current);
    } else if (event.key === '?' && !typing) {
      event.preventDefault(); Keys.show();
    } else if (event.key === 'Escape') {
      if (Keys.open) Keys.hide();
      else if (!$('palette-wrap').hidden) Palette.hide();
      else if (Studio.open) Studio.hide();
      else if (state.busy) { api('/api/interrupt', {}).catch(() => {}); toast('Interrupted.'); }
    } else if (!typing && event.key.length === 1) {
      Composer.input.focus();     // start typing anywhere, land in the composer
    }
  });

  connect();
  Composer.input.focus();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
