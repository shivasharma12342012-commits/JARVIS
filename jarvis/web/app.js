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
        this.push(changes, true);
      });
      $(id).addEventListener('change', () => this.flush());
    });
    $('s-radius').addEventListener('input', () => this.push({ radius: Number($('s-radius').value) }, true));
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
    $('s-tint').value = Math.round((theme.tint == null ? 0.55 : theme.tint) * 100);
    $('s-contrast').value = Math.round((theme.contrast == null ? 0.5 : theme.contrast) * 100);
    $('s-glow').value = Math.round((theme.glow == null ? 0.6 : theme.glow) * 100);
    $('s-radius').value = theme.radius == null ? 14 : theme.radius;

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
  user: (d) => Transcript.message('user', d.text, false),
  agent: (d) => Transcript.message('agent', d.text, d.markdown !== false),
  system: (d) => Transcript.note(d.text, d.level),
  interim: (d) => { if (d.text) Transcript.thought(d.text); },
  thought: (d) => { if (d.text) Transcript.thought(d.text); },
  tool_start: (d) => Transcript.toolStart(d.name, d.arguments),
  tool_end: (d) => Transcript.toolEnd(d.name, d.ok, d.summary),
  alert: (d) => {
    Transcript.note(d.title + ': ' + d.text, d.severity === 'critical' ? 'error' : 'warn');
    toast(d.title);
  },
  code: (d) => Transcript.message('agent', '```' + (d.language || '') + '\n' + d.code + '\n```', true),
  table: (d) => Transcript.table(d.title, d.columns, d.rows),
  clear: () => Transcript.clear(),
  stream_begin: () => streamBegin(),
  token: (d) => streamToken(d.text),
  stream_end: (d) => streamEnd(d.text, d.interim),
  state: (d) => setState(d.state),
  telemetry: (d) => Gauges.update(d),
  metrics: (d) => { if (d.summary) $('metrics-line').textContent = d.summary; },
  model_status: (d) => { $('model-name').textContent = d.text; },
  voice_status: (d) => { $('machine-note').textContent = d.text; },
  protocol: (d) => { if (d.name) toast('Protocol: ' + d.name); },
  warm: () => {},
  amplitude: () => {},
  theme: (d) => { applyVariables(d.variables); Studio.sync(d.theme); },
  ask: (d) => Ask.card(d),
  ask_done: (d) => Ask.close(d.token),
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

  source.onopen = () => { retryDelay = 700; };
  source.onerror = () => {
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
  $('status-label').textContent = labels[next] || next || 'Ready';
  state.busy = !!next && next !== 'idle';
  $('btn-stop').hidden = !state.busy;
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

    if (data.platform || data.processes != null) {
      const uptime = data.uptime ? ', up ' + Math.floor(data.uptime / 3600) + 'h' : '';
      const processes = data.processes != null ? ' - ' + data.processes + ' processes' : '';
      $('machine-note').textContent = (data.platform || '') + processes + uptime;
    }
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
      { label: 'Instruments panel', what: 'Show or hide the right-hand rail', run: () => toggleRail() },
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
   Chrome
   ============================================================================= */
function toast(text) {
  const node = el('div', 'toast', text);
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 260);
  }, 2800);
}

function toggleRail() {
  const open = $('shell-root').classList.toggle('rail-open');
  $('btn-rail').classList.toggle('on', open);
  try { localStorage.setItem('jarvis.rail', open ? '1' : '0'); } catch (err) { /* private mode */ }
}

const SUGGESTIONS = [
  'What is this machine doing right now?',
  'Summarise the files in my workspace',
  'Run a diagnostics sweep',
  'Make the interface violet',
];

function boot() {
  document.querySelector('.shell').id = 'shell-root';
  $('brand-name').textContent = BOOT.agent || 'J.A.R.V.I.S.';
  $('brand-sub').textContent = BOOT.fullName || '';
  $('welcome-title').textContent = BOOT.title || 'Sir';
  $('model-name').textContent = BOOT.model || '-';
  $('model-host').textContent = BOOT.host || '';
  document.title = (BOOT.agent || 'J.A.R.V.I.S.') + ' Desktop';

  const tools = BOOT.tools || [];
  $('tool-count').textContent = String(tools.length);
  tools.forEach((name) => $('tool-list').appendChild(el('li', '', name)));
  (BOOT.protocols || []).forEach((name) => {
    const chip = el('li', 'action', name);
    chip.title = 'Run the ' + name + ' protocol';
    chip.onclick = () => Composer.send('/protocol ' + name);
    $('protocol-list').appendChild(chip);
  });

  SUGGESTIONS.forEach((text) => {
    const button = el('button', 'suggestion', text);
    button.onclick = () => Composer.send(text);
    $('suggestions').appendChild(button);
  });

  applyVariables(BOOT.variables);
  Transcript.init();
  Composer.init();
  Palette.init();
  Studio.init();
  setState(BOOT.state || 'idle');

  let railOpen = window.innerWidth > 1100;
  try {
    const saved = localStorage.getItem('jarvis.rail');
    if (saved !== null) railOpen = saved === '1';
  } catch (err) { /* private mode */ }
  if (railOpen) toggleRail();

  $('btn-palette').onclick = () => Studio.toggle();
  $('btn-command').onclick = () => Palette.show();
  $('btn-rail').onclick = () => toggleRail();
  $('scrim').onclick = () => Studio.hide();

  document.addEventListener('keydown', (event) => {
    const meta = event.ctrlKey || event.metaKey;
    if (meta && event.key.toLowerCase() === 'k') { event.preventDefault(); Palette.show(); }
    else if (meta && event.key === '/') { event.preventDefault(); Studio.toggle(); }
    else if (meta && event.key.toLowerCase() === 'b') { event.preventDefault(); toggleRail(); }
    else if (event.key === 'Escape') {
      if (!$('palette-wrap').hidden) Palette.hide();
      else if (Studio.open) Studio.hide();
      else if (state.busy) { api('/api/interrupt', {}).catch(() => {}); toast('Interrupted.'); }
    } else if (!event.target.closest('input, textarea') && event.key.length === 1) {
      Composer.input.focus();     // start typing anywhere, land in the composer
    }
  });

  connect();
  Composer.input.focus();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
