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
    // Kept aside so /clear can put it back. Removing it on the first message
    // and never restoring it left an empty grey pane after every reset.
    const welcome = $('welcome');
    this.welcome = welcome ? welcome.cloneNode(true) : null;
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
  /* `force` is the Jump button: a deliberate move, worth a glide. Everything
     else is a stream following itself, which must be instant. */
  toBottom(force) {
    if (!force && !state.atBottom) return;
    requestAnimationFrame(() => {
      this.node.scrollTo({
        top: this.node.scrollHeight,
        behavior: (force && !REDUCED) ? 'smooth' : 'instant',
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
    state.lastTool = null;
    if (this.welcome) {
      const fresh = this.welcome.cloneNode(true);
      this.node.appendChild(fresh);
      // The suggestions are buttons; the clone lost their handlers.
      fresh.querySelectorAll('.suggestion').forEach((button) => {
        button.onclick = () => Composer.send(button.textContent);
      });
    }
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
      { label: 'Code', what: 'The editor, the tree and the terminal',
        run: () => Ide.surface('code') },
      { label: 'Open a file\u2026', what: 'Find a file by name in the workspace',
        run: () => { Ide.surface('code'); setTimeout(() => Ide.quick.show(), 60); } },
      { label: 'Search the workspace', what: 'Find text across every file',
        run: () => { Ide.surface('code'); setTimeout(() => Ide.panels.show('search'), 60); } },
      { label: 'Terminal', what: 'The system shell', run: () => Rail.show('terminal') },
      { label: 'Agent', what: 'The agentic HUD, live', run: () => Rail.show('agent') },
      { label: 'Logs', what: 'This session as it happens', run: () => Rail.show('logs') },
      { label: 'System', what: 'Instruments and machine gauges', run: () => Rail.show('system') },
      { label: 'Right panel', what: 'Show or hide it', run: () => Shell.toggle('rail') },
      { label: 'Sidebar', what: 'Show or hide it', run: () => Shell.toggle('sidebar') },
      { label: 'Restart the shell', what: 'Start a fresh shell process', run: () => {
        Rail.show('terminal');
        api('/api/shell', { restart: true }).catch(() => {});
      } },
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

  /* A width. Zero is never a legitimate stored width, so it falls back. */
  read(key, fallback) {
    try {
      const saved = Number(localStorage.getItem('jarvis.' + key));
      return Number.isFinite(saved) && saved > 0 ? saved : fallback;
    } catch (err) { return fallback; }
  },

  /* A flag. Zero *is* legitimate — it means the operator closed that pane —
     so this must distinguish a stored 0 from an absent key, which `read`
     above cannot. Without it, closing a pane never survived a reload. */
  readFlag(key, fallback) {
    try {
      const raw = localStorage.getItem('jarvis.' + key);
      if (raw === null || raw === '') return fallback;
      return raw === '1' || raw === 'true';
    } catch (err) { return fallback; }
  },

  write(key, value) {
    try { localStorage.setItem('jarvis.' + key, String(value)); } catch (err) { /* private mode */ }
  },

  restore() {
    const root = document.documentElement;
    root.style.setProperty('--sidebar-width', this.read('sidebar', 244) + 'px');
    root.style.setProperty('--rail-width', this.read('rail', 400) + 'px');
    const wide = window.innerWidth > 1180;
    this.set('sidebar', this.readFlag('sidebar-open', wide));
    this.set('rail', this.readFlag('rail-open', wide));
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
const VIEWS = ['terminal', 'agent', 'logs', 'system'];

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
    // Selects the remembered tab without forcing the rail open: restoring which
    // view was last shown must not override whether the pane was closed. It did,
    // which meant a narrow window landed on a full-screen Terminal with the
    // conversation behind it and no obvious way back.
    this.show(this.remembered(), { silent: true });
  },

  remembered() {
    try {
      const saved = localStorage.getItem('jarvis.view');
      return VIEWS.indexOf(saved) === -1 ? 'terminal' : saved;
    } catch (err) { return 'terminal'; }
  },

  show(view, options) {
    if (VIEWS.indexOf(view) === -1) return;
    this.view = view;
    if (!(options && options.silent)) Shell.open('rail');
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
    if (options && options.silent) return;
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
        el('span', 'log-msg', line.message),
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
        Ide.open(entry.path);
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
   Where the code viewer used to be

   Reading a file, colouring it and editing it all moved to the Code surface —
   `lang.js` for the grammar, `ide.js` for the editor. The conversation keeps
   fenced code blocks and nothing else, which is the right amount of code for a
   pane you read prose in. Opening a file from the tree hands off to the editor.
   ============================================================================= */

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
    Rail.mark('agent');      // this pane is Agent; Terminal is the system shell
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
  const host = $('toasts');
  // The same notice twice in a row is one notice. Repeating it just restarts
  // the clock on the one already showing.
  const last = host.lastElementChild;
  if (last && last.textContent === text && !last.classList.contains('leaving')) {
    clearTimeout(Number(last.dataset.timer));
    last.dataset.timer = String(setTimeout(() => fade(last), 2600));
    return;
  }
  const node = el('div', 'toast', text);
  host.appendChild(node);
  // Three at once is already more than anyone reads.
  while (host.children.length > 3) host.removeChild(host.firstChild);
  node.dataset.timer = String(setTimeout(() => fade(node), 2600));

  function fade(target) {
    target.classList.add('leaving');
    setTimeout(() => target.remove(), 220);
  }
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
  ['Everywhere', [
    ['Command palette', ['Ctrl', 'K']],
    ['Chat \u2194 Code', ['Ctrl', 'Shift', 'C']],
    ['Appearance', ['Ctrl', ',']],
    ['Interrupt the turn', ['Ctrl', '.']],
    ['Close what is open', ['Esc']],
    ['This sheet', ['?']],
  ]],
  ['Conversation', [
    ['Send', ['Enter']],
    ['Newline', ['Shift', 'Enter']],
    ['Command completion', ['/']],
    ['New session', ['Ctrl', 'N']],
    ['Right panel', ['Ctrl', 'B']],
    ['Sidebar', ['Ctrl', '\\']],
    ['Terminal \u00b7 Agent \u00b7 Logs \u00b7 System', ['Ctrl', '1\u20134']],
  ]],
  ['Code \u00b7 getting around', [
    ['Open a file by name', ['Ctrl', 'P']],
    ['Search the workspace', ['Ctrl', 'Shift', 'F']],
    ['Explorer \u00b7 Outline \u00b7 Problems \u00b7 Agent', ['Ctrl', 'Shift', 'E/O/M/A']],
    ['Side panel', ['Ctrl', 'B']],
    ['Mission panel', ['Ctrl', 'Shift', 'B']],
    ['Bottom dock', ['Ctrl', 'J']],
    ['Terminal', ['Ctrl', '`']],
    ['Close the file', ['Alt', 'W']],
  ]],
  ['Code \u00b7 editing', [
    ['Save', ['Ctrl', 'S']],
    ['Find', ['Ctrl', 'F']],
    ['Replace', ['Ctrl', 'H']],
    ['Go to line', ['Ctrl', 'G']],
    ['Toggle comment', ['Ctrl', '/']],
    ['Duplicate', ['Ctrl', 'D']],
    ['Delete the line', ['Ctrl', 'Shift', 'K']],
    ['Move the line', ['Alt', '\u2191', '\u2193']],
    ['Indent \u00b7 outdent', ['Tab', 'Shift', 'Tab']],
    ['Soft wrap', ['Alt', 'Z']],
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

  // A suggestion lands in the composer with the caret after it. Firing it on
  // the click gave you no chance to change a word first, which made the whole
  // row something to avoid rather than something to use.
  SUGGESTIONS.forEach((text) => {
    const button = el('button', 'suggestion', text);
    button.onclick = () => {
      Composer.input.value = text;
      Composer.autosize();
      Composer.input.focus();
      Composer.input.setSelectionRange(text.length, text.length);
    };
    $('suggestions').appendChild(button);
  });

  applyVariables(BOOT.variables);
  Shell.init();
  Transcript.init();
  Composer.init();
  Palette.init();
  Studio.init();
  Term.init();
  Logs.init();
  Sh.init();
  Rail.init();
  Tree.init();
  Sessions.init();
  Keys.init();
  setState(BOOT.state || 'idle');

  // Only shown when there is something to sign out of. With the lock off the
  // button would just be a control that does nothing.
  const account = BOOT.auth || {};
  if (account.required) {
    const signout = $('btn-signout');
    signout.hidden = false;
    const who = (BOOT.identity && BOOT.identity.subject) || '';
    signout.title = who ? 'Sign out (' + who + ')' : 'Sign out';
    signout.onclick = () => {
      api('/api/auth/logout', {})
        .then(() => location.replace('/'))
        .catch(() => location.replace('/'));
    };
  }

  $('btn-palette').onclick = () => Studio.toggle();
  $('btn-command').onclick = () => Palette.show();
  $('btn-rail').onclick = () => Shell.toggle('rail');
  $('btn-sidebar').onclick = () => Shell.toggle('sidebar');
  $('scrim').onclick = () => Studio.hide();
  // "+" means "bring a file into this conversation", so it opens the file
  // picker. It used to throw two panes open instead, which is not a thing
  // anybody pressed it hoping for.
  $('btn-attach').onclick = () => { Ide.surface('code'); setTimeout(() => Ide.quick.show(), 60); };
  $('model-chip').onclick = () => { Rail.show('system'); };
  $('btn-mic').onclick = () => Composer.send('talk');
  $('btn-mute').onclick = () => Composer.send('quiet');
  document.querySelectorAll('.nav-item[data-rail]').forEach((item) => {
    item.onclick = () => Rail.show(item.dataset.rail);
  });
  $('btn-goto-code').onclick = () => Ide.surface('code');

  /* The conversation's keyboard map.

     Four things here were changed because living with them was worse than
     living without them:

       · Ctrl+/ used to open the appearance panel. Ctrl+/ toggles a comment in
         every editor ever written, and the Code surface needs it for that, so
         appearance moved to Ctrl+, — which is where settings live anyway.

       · Ctrl+W used to close a file. In a browser and in most desktop webviews
         Ctrl+W closes the window, taking the session with it. Nothing is worth
         that; closing a tab is Alt+W on the Code surface.

       · Escape used to interrupt a running turn when no panel was open. An
         Escape is a reflex — you press it to dismiss things — and having a
         reflex kill work in progress is indefensible. Interrupting is Ctrl+.
         and the Stop button, both of which you have to mean.

       · Any printable key used to pull focus into the composer. It also ate
         that key, because the keypress had already been delivered somewhere
         else: typing "hello" into the page gave you "ello". It now inserts the
         character it captured, and only when nothing else could want it.       */
  document.addEventListener('keydown', (event) => {
    const meta = event.ctrlKey || event.metaKey;
    const typing = !!(event.target.closest && event.target.closest('input, textarea, [contenteditable]'));
    const overlay = Keys.open || !$('palette-wrap').hidden || Studio.open ||
                    !$('quick-wrap').hidden;

    if (event.key === 'Escape') {
      if (Keys.open) Keys.hide();
      else if (!$('palette-wrap').hidden) Palette.hide();
      else if (Studio.open) Studio.hide();
      return;
    }
    if (meta && event.key === ',') { event.preventDefault(); Studio.toggle(); return; }
    if (meta && event.key === '.') {
      event.preventDefault();
      if (state.busy) { api('/api/interrupt', {}).catch(() => {}); toast('Interrupted.'); }
      return;
    }
    // `!shiftKey` matters: Ctrl+Shift+K deletes a line in the editor, and without
    // this the palette opened on top of it every time.
    if (meta && !event.shiftKey && event.key.toLowerCase() === 'k') {
      event.preventDefault(); Palette.show(); return;
    }

    // Everything below belongs to the conversation. The Code surface has its
    // own map and registers it separately.
    if (document.body.dataset.surface === 'code') return;

    if (meta && event.key.toLowerCase() === 'b' && !event.shiftKey) {
      event.preventDefault(); Shell.toggle('rail'); return;
    }
    if (meta && event.key === '\\') { event.preventDefault(); Shell.toggle('sidebar'); return; }
    if (meta && event.key.toLowerCase() === 'n') { event.preventDefault(); Sessions.create(); return; }
    if (meta && event.key >= '1' && event.key <= String(VIEWS.length)) {
      event.preventDefault(); Rail.show(VIEWS[Number(event.key) - 1]); return;
    }
    if (event.key === '?' && !typing && !overlay) { event.preventDefault(); Keys.show(); return; }

    // Start typing anywhere and land in the composer — with the character you
    // typed, which is the whole point and what the old version dropped.
    if (!typing && !overlay && !meta && !event.altKey && event.key.length === 1) {
      event.preventDefault();
      Composer.input.focus();
      Composer.input.value += event.key;
      Composer.autosize();
    }
  });

  connect();
  Composer.input.focus();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
