// Let There: point at anything in an app you build, press \, say what should change.
//
// The app loads this with one tag: <script src="http://localhost:3470/overlay.js" data-app="NAME" defer></script>.
// Rest the pointer on something and a thin frame marks it; \ locks the frame (it types a normal backslash while
// you type in a field) and opens a glass box beside it with the cursor inside, so Superwhisper types straight
// into it. ↑ (while the box is empty) widens the frame to the surrounding element and ↓ narrows it back; Enter
// saves, Esc closes. The note goes to the server this script came from (letthere/server.py) with where the
// element is (CSS path, box), its text, an HTML excerpt, and a picture of it.
//
// Everything lives in one Shadow DOM, so the app's styles cannot reach the overlay and the overlay's cannot
// reach the app. Keys typed in the box stop there: the app's own shortcuts never see them.
(() => {
  if (window.letThere) return;
  const script = document.currentScript;
  const server = new URL(script.src).origin;
  const app = script.dataset.app || location.host;
  window.letThere = {app, server};

  const DWELL = 300;       // ms the pointer rests before the frame shows
  const PICTURE_MAX = 1600; // widest picture side, in pixels
  const PICTURE_NODES = 3000;

  const host = document.createElement('let-there');
  host.style.cssText = 'all: initial; position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;';
  const root = host.attachShadow({mode: 'open'});
  root.innerHTML = `<style>
:host { all: initial; }
.frame { position: fixed; pointer-events: none; border-radius: 4px; transition: all 90ms ease-out;
  outline: 1px dashed rgb(120 190 255 / 0.75); box-shadow: 0 0 0 1px rgb(0 0 0 / 0.18); }
.frame.locked { outline: 1.5px solid rgb(120 190 255); box-shadow: 0 0 0 1px rgb(0 0 0 / 0.3), 0 0 0 5px rgb(120 190 255 / 0.18); }
[hidden] { display: none !important; }

/* Glass tokens (liquid-glass-ui): the only place the material's values live. The shell is glass; what you read
   and type sits on a solid surface, because the app's background is not ours to choose. */
.box {
  --glass-tint-alpha: 0.14;
  --glass-tint: rgb(255 255 255 / var(--glass-tint-alpha));
  --glass-blur: blur(2px) saturate(180%) brightness(1.06);
  --glass-lens: ;
  --glass-edge: 0 10px 30px rgb(0 0 0 / 0.14), inset 1px 1px 0 rgb(255 255 255 / 0.55),
    inset -1px -1px 0 rgb(255 255 255 / 0.22), inset 0 0 0 3px rgb(255 255 255 / 0.06);
  --glass-ink: rgb(20 20 24 / 0.86);
  --glass-ink-blend: multiply;
  --solid: rgb(255 255 255 / 0.94);
  --solid-ink: rgb(20 20 24);
  --solid-faint: rgb(20 20 24 / 0.55);
}
.box[data-glass='dark'] {
  --glass-tint-alpha: 0.27;
  --glass-tint: rgb(20 20 24 / var(--glass-tint-alpha));
  --glass-blur: blur(2px) saturate(160%) brightness(0.8);
  --glass-edge: 0 10px 30px rgb(0 0 0 / 0.35), inset 1px 1px 0 rgb(255 255 255 / 0.22),
    inset -1px -1px 0 rgb(255 255 255 / 0.08), inset 0 0 0 3px rgb(255 255 255 / 0.03);
  --glass-ink: rgb(255 255 255 / 0.92);
  --glass-ink-blend: overlay;
  --solid: rgb(24 26 30 / 0.94);
  --solid-ink: rgb(240 242 245);
  --solid-faint: rgb(240 242 245 / 0.55);
}
@supports not (backdrop-filter: blur(1px)) {
  .box { --glass-tint-alpha: 0.22; }
  .box[data-glass='dark'] { --glass-tint-alpha: 0.38; }
}
@media (prefers-reduced-transparency: reduce) {
  .box { --glass-tint-alpha: 0.92; }
  .box[data-glass='dark'] { --glass-tint-alpha: 0.92; }
}
.glass {
  background: var(--glass-tint);
  backdrop-filter: var(--glass-blur) var(--glass-lens);
  -webkit-backdrop-filter: var(--glass-blur);
  box-shadow: var(--glass-edge);
  color: var(--glass-ink);
}

.box { position: fixed; width: 340px; padding: 6px; border-radius: 16px; pointer-events: auto; box-sizing: border-box;
  font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
textarea { display: block; width: 100%; box-sizing: border-box; min-height: 52px; max-height: 160px; resize: none;
  margin: 0; padding: 8px 10px; border: 0; border-radius: 11px; outline: none; font: inherit; font-size: 14px;
  background: var(--solid); color: var(--solid-ink); }
textarea::placeholder { color: var(--solid-faint); }
.foot { display: flex; gap: 8px; align-items: baseline; padding: 5px 6px 1px; font-size: 11px; }
.foot .what { font-family: ui-monospace, "SF Mono", Menlo, monospace; mix-blend-mode: var(--glass-ink-blend);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.foot .keys { mix-blend-mode: var(--glass-ink-blend); white-space: nowrap; }
.status { margin: 6px 0 0; padding: 6px 10px; border-radius: 9px; background: var(--solid); color: var(--solid-ink); }
.status.error { color: rgb(200 40 40); }
.box[data-glass='dark'] .status.error { color: rgb(255 130 120); }
</style>
<div class="frame" hidden></div>
<div class="box glass" role="dialog" aria-label="Let There: what should change here?" hidden>
  <textarea rows="2" placeholder="Say what should change" aria-label="What should change"></textarea>
  <div class="foot"><span class="what"></span><span class="keys">↑ wider · ↵ save · esc</span></div>
  <p class="status" role="status" hidden></p>
</div>`;
  const frame = root.querySelector('.frame');
  const box = root.querySelector('.box');
  const field = root.querySelector('textarea');
  const what = root.querySelector('.what');
  const status = root.querySelector('.status');
  document.documentElement.append(host);

  let hovered = null;   // the element under the resting pointer
  let locked = null;    // the element the note is about
  let narrower = [];    // what ↑ widened from, for ↓
  let pointer = null;   // the last pointer position
  let dwell = 0;
  let saving = false;

  const ours = event => event.composedPath().includes(host);
  const typing = event => event.composedPath()[0]?.closest?.('input, textarea, select, [contenteditable]:not([contenteditable="false"])');
  const pointable = element => element && element !== document.documentElement && element !== document.body
    && element !== host && element.nodeType === Node.ELEMENT_NODE;

  function place(element) {
    const rect = element.getBoundingClientRect();
    Object.assign(frame.style, {left: `${rect.left - 2}px`, top: `${rect.top - 2}px`,
                                width: `${rect.width + 4}px`, height: `${rect.height + 4}px`});
    frame.hidden = false;
    if (!locked) return;
    const width = box.offsetWidth || 340;
    const height = box.offsetHeight || 110;
    const below = rect.bottom + 10;
    const top = below + height <= innerHeight ? below
      : rect.top - 10 - height >= 0 ? rect.top - 10 - height
      : Math.max(8, innerHeight - height - 8);
    box.style.left = `${Math.min(Math.max(8, rect.left), innerWidth - width - 8)}px`;
    box.style.top = `${top}px`;
  }

  function hide() {
    clearTimeout(dwell);
    if (!locked) frame.hidden = true;
  }

  function label(element) {
    const classes = [...element.classList].slice(0, 2).map(name => `.${name}`).join('');
    const text = (element.innerText || '').trim().replace(/\s+/g, ' ');
    return `${element.tagName.toLowerCase()}${classes}${text ? ` · ${text.slice(0, 40)}` : ''}`;
  }

  // Light or dark glass from what is behind the box: the average luminance of the first opaque background
  // under five points, against 0.5. A dark background gets dark glass with light ink, the readable reading.
  function tone() {
    const rect = box.getBoundingClientRect();
    const points = [[0.5, 0.5], [0.1, 0.1], [0.9, 0.1], [0.1, 0.9], [0.9, 0.9]];
    let total = 0;
    for (const [x, y] of points) {
      const under = document.elementsFromPoint(rect.left + rect.width * x, rect.top + rect.height * y)
        .filter(element => element !== host);
      total += luminance(background(under));
    }
    box.dataset.glass = total / points.length < 0.5 ? 'dark' : 'light';
  }

  function background(elements) {
    for (const element of elements) {
      const color = getComputedStyle(element).backgroundColor;
      const parts = color.match(/[\d.]+/g)?.map(Number) || [];
      if (parts.length >= 3 && (parts[3] ?? 1) > 0.5) return parts;
    }
    return [255, 255, 255];
  }

  function luminance([r, g, b]) {
    const linear = value => (value /= 255) <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  }

  function lock(element) {
    clearTimeout(dwell);
    locked = element;
    narrower = [];
    frame.classList.add('locked');
    status.hidden = true;
    box.hidden = false;
    what.textContent = label(element);
    place(element);
    tone();
    field.focus();
  }

  function release() {
    locked = null;
    narrower = [];
    saving = false;
    field.value = '';
    field.disabled = false;
    box.hidden = true;
    frame.classList.remove('locked');
    frame.hidden = true;
  }

  function retarget(element) {
    locked = element;
    what.textContent = label(element);
    place(element);
  }

  document.addEventListener('mousemove', event => {
    pointer = [event.clientX, event.clientY];
    if (locked || ours(event)) return;
    const element = event.composedPath()[0];
    if (!pointable(element)) return hide();
    if (element === hovered && !frame.hidden) return;
    hovered = element;
    clearTimeout(dwell);
    if (!frame.hidden) place(element);
    else dwell = setTimeout(() => place(element), DWELL);
  }, {capture: true, passive: true});
  document.documentElement.addEventListener('mouseleave', hide);
  addEventListener('scroll', () => (locked ? place(locked) : hide()), {capture: true, passive: true});
  addEventListener('resize', () => locked && place(locked), {passive: true});
  document.addEventListener('mousedown', event => {
    if (locked && !ours(event) && !field.value.trim() && !saving) release();
  }, true);

  addEventListener('keydown', event => {
    if (event.isComposing || ours(event)) return;
    if (event.key === 'Escape' && locked && !saving) {
      event.preventDefault();
      release();
      return;
    }
    if (event.key !== '\\' || event.metaKey || event.ctrlKey || event.altKey || typing(event)) return;
    const element = locked ? null
      : !frame.hidden && hovered ? hovered
      : pointer ? document.elementsFromPoint(...pointer).find(candidate => candidate !== host) : null;
    if (!pointable(element)) return;
    event.preventDefault();
    event.stopPropagation();
    lock(element);
  }, true);

  // Keys typed in the box belong to the box: stop them here so the app's shortcuts (j, k, /, ...) stay quiet.
  field.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.isComposing || saving) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      release();
    } else if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      save();
    } else if (event.key === 'ArrowUp' && !field.value && pointable(locked.parentElement)) {
      event.preventDefault();
      narrower.push(locked);
      retarget(locked.parentElement);
    } else if (event.key === 'ArrowDown' && !field.value && narrower.length) {
      event.preventDefault();
      retarget(narrower.pop());
    }
  });
  field.addEventListener('input', () => {
    field.style.height = 'auto';
    field.style.height = `${field.scrollHeight}px`;
    if (locked) place(locked);
  });

  function say(message, error = false) {
    status.textContent = message;
    status.classList.toggle('error', error);
    status.hidden = false;
    if (locked) place(locked);
  }

  async function save() {
    const note = field.value.trim();
    if (!note) return;
    if (!locked.isConnected) return say('That element left the page. Press Esc and point at it again.', true);
    saving = true;
    field.disabled = true;
    say('Saving…');
    const element = locked;
    const rect = element.getBoundingClientRect();
    const payload = {
      app, note,
      page: location.href,
      title: document.title,
      selector: selector(element),
      text: (element.innerText || element.textContent || '').trim().slice(0, 4000),
      html: element.outerHTML.slice(0, 6000),
      box: {left: Math.round(rect.left + scrollX), top: Math.round(rect.top + scrollY),
            width: Math.round(rect.width), height: Math.round(rect.height)},
      viewport: {width: innerWidth, height: innerHeight},
      picture: await picture(element).catch(() => null),
    };
    try {
      const response = await fetch(`${server}/notes`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      const answer = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(answer.error || `Let There answered ${response.status}`);
      say(`Saved as ${answer.id}`);
      setTimeout(() => locked === element && release(), 1400);
    } catch (error) {
      saving = false;
      field.disabled = false;
      field.focus();
      say(error instanceof TypeError ? `Let There is not answering at ${server}. Start letthere/server.py; your words are still here.`
                                     : error.message, true);
    }
  }

  // The element's CSS path: from the nearest ancestor with a unique id (or body) down, each step a tag with up
  // to two classes and, where siblings share the tag, its place among them.
  function selector(element) {
    const steps = [];
    for (let node = element; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
      if (node.id && document.querySelectorAll(`#${CSS.escape(node.id)}`).length === 1) {
        steps.unshift(`#${CSS.escape(node.id)}`);
        return steps.join(' > ');
      }
      let step = node.tagName.toLowerCase() + [...node.classList].slice(0, 2).map(name => `.${CSS.escape(name)}`).join('');
      const same = node.parentElement ? [...node.parentElement.children].filter(child => child.tagName === node.tagName) : [];
      if (same.length > 1) step += `:nth-of-type(${same.indexOf(node) + 1})`;
      steps.unshift(step);
    }
    return ['body', ...steps].join(' > ');
  }

  // A picture of the element, drawn in the page: a copy of it with every computed style inlined, rendered
  // through an SVG foreignObject onto a canvas. Same-origin images are inlined; other sites' images, CSS
  // background images, and ::before/::after content are left out (the page cannot read them into a canvas).
  async function picture(element) {
    const rect = element.getBoundingClientRect();
    const width = Math.ceil(rect.width);
    const height = Math.ceil(rect.height);
    if (!width || !height) return null;
    const budget = {nodes: PICTURE_NODES};
    const copy = await copyOf(element, budget);
    Object.assign(copy.style, {margin: '0', position: 'relative', inset: 'auto', transform: 'none'});
    const markup = new XMLSerializer().serializeToString(copy);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">`
      + `<foreignObject x="0" y="0" width="100%" height="100%">${markup}</foreignObject></svg>`;
    const image = new Image();
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    await image.decode();
    const scale = Math.min(devicePixelRatio || 1, 2, PICTURE_MAX / Math.max(width, height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext('2d');
    context.fillStyle = `rgb(${background([element, ...ancestors(element)]).slice(0, 3).join(' ')})`;
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.scale(scale, scale);
    context.drawImage(image, 0, 0);
    return canvas.toDataURL('image/png');
  }

  function ancestors(element) {
    const list = [];
    for (let node = element.parentElement; node; node = node.parentElement) list.push(node);
    return list;
  }

  async function copyOf(node, budget) {
    if (node.nodeType === Node.TEXT_NODE) return node.cloneNode();
    if (node.nodeType !== Node.ELEMENT_NODE || budget.nodes-- <= 0) return null;
    if (['SCRIPT', 'STYLE', 'LINK', 'TEMPLATE', 'NOSCRIPT', 'LET-THERE'].includes(node.tagName)) return null;
    const copy = node.cloneNode(false);
    const style = getComputedStyle(node);
    for (const name of style) {
      const value = style.getPropertyValue(name);
      if (!value.includes('url(')) copy.style.setProperty(name, value);
    }
    if (node instanceof HTMLImageElement) {
      copy.removeAttribute('srcset');
      copy.removeAttribute('loading');
      const inlined = await inline(node.currentSrc || node.src);
      if (inlined) copy.setAttribute('src', inlined);
      else copy.removeAttribute('src');
    } else if (node instanceof HTMLCanvasElement) {
      try {
        const image = document.createElement('img');
        image.setAttribute('src', node.toDataURL());
        image.setAttribute('style', copy.getAttribute('style'));
        return image;
      } catch {
        return copy;
      }
    } else if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
      copy.setAttribute('value', node.value);
      if (node instanceof HTMLTextAreaElement) copy.textContent = node.value;
    }
    for (const child of node.childNodes) {
      const part = await copyOf(child, budget);
      if (part) copy.append(part);
    }
    return copy;
  }

  async function inline(source) {
    if (!source) return null;
    if (source.startsWith('data:')) return source;
    try {
      if (new URL(source, location.href).origin !== location.origin) return null;
      const blob = await (await fetch(source)).blob();
      return await new Promise(resolve => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(blob);
      });
    } catch {
      return null;
    }
  }
})();
