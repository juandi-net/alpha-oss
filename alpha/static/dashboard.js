// Alpha dashboard: live search and filters on the Feed, more results in place, α / β saved without a reload, the
// Skills filter, buttons that say what they are doing while a slow request runs, the live run ledger, and keyboard
// shortcuts. Every link and form also works without it.

const results = document.getElementById('results');
const search = document.getElementById('q');

if (results && search) {
  let controller;
  let timer;

  // Fetch a Feed URL and swap in its results and filter counts (or append its rows for "Show more").
  async function load(url, { append = false, push = false } = {}) {
    controller?.abort();
    controller = new AbortController();
    document.body.classList.add('busy');
    try {
      const response = await fetch(url, { signal: controller.signal });
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const fresh = page.getElementById('results');
      if (append) {
        results.querySelector('.rows').append(...fresh.querySelectorAll('.rows > li'));
        const more = fresh.querySelector('.more');
        const old = results.querySelector('.more');
        if (more) old.replaceWith(more); else old.remove();
      } else {
        results.innerHTML = fresh.innerHTML;
        document.querySelector('.facet-groups').innerHTML = page.querySelector('.facet-groups').innerHTML;
        document.title = page.title;
        history[push ? 'pushState' : 'replaceState'](null, '', url);
      }
    } catch (error) {
      if (error.name !== 'AbortError') location.href = url;
    } finally {
      document.body.classList.remove('busy');
    }
  }

  function searchUrl() {
    const url = new URL(location.href);
    const words = search.value.trim();
    if (words) url.searchParams.set('q', words); else url.searchParams.delete('q');
    url.searchParams.delete('offset');
    return url.pathname + (url.search || '');
  }

  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(searchUrl()), 220);
  });
  search.form.addEventListener('submit', event => {
    event.preventDefault();
    clearTimeout(timer);
    load(searchUrl());
  });
  search.addEventListener('keydown', event => {
    if (event.key === 'Escape' && search.value) {
      search.value = '';
      load(searchUrl());
    }
  });

  document.addEventListener('click', event => {
    const link = event.target.closest('.facet a, .pills a, #results .empty a, a.more');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    if (link.classList.contains('more')) {
      link.textContent = 'Loading…';
      load(link.href, { append: true });
      return;
    }
    const url = new URL(link.href);
    search.value = url.searchParams.get('q') || '';
    load(url.pathname + url.search, { push: true });
  });

  window.addEventListener('popstate', () => {
    search.value = new URL(location.href).searchParams.get('q') || '';
    load(location.pathname + location.search);
  });
}

// The status dot and its run ledger stay current without a reload: asked again every 5 s while a run or build is
// going, every 30 s otherwise, and at once when the tab comes back into view. It also says when new items arrive,
// instead of pushing them into a list the eye is reading. An open ledger stays open.
const ledger = document.getElementById('ledger');
if (ledger) {
  const since = ledger.dataset.since;
  let timer;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, document.querySelector('#ledger .pulse.on') ? 5000 : 30000);
  };
  async function refresh() {
    if (document.visibilityState !== 'visible') return;
    try {
      const response = await fetch('/ledger?since=' + encodeURIComponent(since));
      if (response.ok) {
        const template = document.createElement('template');
        template.innerHTML = (await response.text()).trim();
        const fresh = template.content.firstElementChild;
        const current = document.getElementById('ledger');
        if (fresh) fresh.open = current.open;
        if (fresh && fresh.outerHTML !== current.outerHTML) current.replaceWith(fresh);
      }
    } catch {
      // The dashboard restarting; the next round tries again.
    }
    schedule();
  }
  schedule();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refresh();
  });
}

// α / β: each button posts its next state (the active one again clears it), so the form works without this. Here
// the tap shows at once, the button says it is saving, then saved; a failure puts the old state back and says so
// (https://interfacelineage.com/show-every-state). A line then says what the rating does and fades; the button's
// title keeps saying it.
function paint(form, state) {
  form.dataset.rating = state.rating || '';
  form.dataset.reason = state.reason || '';
  for (const button of form.querySelectorAll('button')) {
    const field = button.name === 'rating' ? 'rating' : 'reason';
    const on = (state[field] || '') === button.dataset.value;
    button.value = on ? '' : button.dataset.value;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (field === 'rating') button.title = on ? button.dataset.done : button.dataset.label;
  }
  form.closest('.row, .suggestion')?.classList.toggle('rated-beta', state.rating === 'beta');
}

function say(form, text, failed = false) {
  const line = form.querySelector('.said');
  clearTimeout(line.fade);
  line.textContent = text;
  line.classList.toggle('bad', failed);
  line.classList.add('on');
  line.fade = setTimeout(() => {
    line.classList.remove('on');
    line.fade = setTimeout(() => { line.textContent = ''; }, 700);
  }, failed ? 6000 : 3200);
}

document.addEventListener('submit', async event => {
  const form = event.target.closest('form.rate');
  const button = event.submitter;
  if (!form || !button) return;
  event.preventDefault();
  if (form.classList.contains('pending')) return;
  const before = { rating: form.dataset.rating, reason: form.dataset.reason };
  const rating = button.name === 'rating';
  // The button's next state and address, read before the optimistic paint flips its value. A button without its
  // own formaction posts to the form's action (its formAction property would be the page's address).
  const value = button.value;
  const action = button.getAttribute('formaction') || form.getAttribute('action');
  paint(form, rating ? { rating: value, reason: '' } : { rating: 'beta', reason: value });
  form.classList.add('pending');
  button.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(action, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      body: new URLSearchParams({ [button.name]: value }),
    });
    if (!response.ok) throw new Error(`the dashboard answered ${response.status}`);
    const saved = await response.json();
    paint(form, saved);
    const pressed = form.querySelector(`button[name=rating][data-value="${saved.rating}"]`);
    if (rating) say(form, pressed ? pressed.dataset.done.replace(/^Rated .: /, '') : 'Rating cleared');
    else say(form, saved.reason ? `Noted: ${button.textContent}` : 'Reason cleared');
  } catch (error) {
    paint(form, before);
    say(form, `Not saved (${error.message === 'Failed to fetch' ? 'the dashboard did not answer' : error.message}). Try again.`, true);
  } finally {
    form.classList.remove('pending');
    button.removeAttribute('aria-busy');
  }
});

// Keyboard: j/k move through a list of items, o or Enter opens the selected one, a/b rate it (on an item's page,
// that item), g then a letter goes to a tab, "/" searches the Feed, and ? opens the sheet that lists them all.
const sheet = document.getElementById('keys');
const go = JSON.parse(document.body.dataset.go || '{}');
let selected = null;
let pending = false;
let pendingTimer;

function step(by) {
  const rows = [...document.querySelectorAll('.rows > li.row')].filter(row => row.offsetParent);
  if (!rows.length) return;
  const at = rows.indexOf(selected);
  const next = rows[at < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, at + by))];
  selected?.classList.remove('selected');
  selected = next;
  next.classList.add('selected');
  next.scrollIntoView({ block: 'nearest' });
}

// The keyboard button sits in the run ledger, which the script redraws, so the click is caught on the document.
document.addEventListener('click', event => {
  if (event.target.closest('#keys-open')) sheet.showModal();
});

// The Feed's search and filters wait behind one control; opening it puts the cursor in the search.
const finder = document.getElementById('finder');
finder?.addEventListener('toggle', () => {
  if (finder.open && !search.value) search.focus();
});

document.addEventListener('keydown', event => {
  if (event.metaKey || event.ctrlKey || event.altKey || sheet?.open) return;
  if (event.target.closest('input, textarea, select, [contenteditable]')) return;
  if (pending) {
    pending = false;
    if (go[event.key]) {
      event.preventDefault();
      location.href = go[event.key];
    }
    return;
  }
  const key = event.key;
  // A comparison or a readback is a view to leave: Esc goes back to where it was opened from.
  const leave = document.querySelector('a[data-leave]');
  if (key === 'Escape' && leave) {
    event.preventDefault();
    location.href = leave.href;
    return;
  }
  if (key === 'j' || key === 'k') {
    event.preventDefault();
    step(key === 'j' ? 1 : -1);
  } else if (key === 'a' || key === 'b') {
    const scope = selected?.isConnected ? selected : document.querySelector('.item-head');
    const button = scope?.querySelector(`.rate button[data-value=${key === 'a' ? 'alpha' : 'beta'}]`);
    if (button) {
      event.preventDefault();
      button.form.requestSubmit(button);
    }
  } else if ((key === 'o' || (key === 'Enter' && event.target === document.body)) && selected?.isConnected) {
    event.preventDefault();
    selected.querySelector('h3 a').click();
  } else if (key === 'g') {
    pending = true;
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => { pending = false; }, 1500);
  } else if (key === '?') {
    event.preventDefault();
    sheet.showModal();
  } else if (key === '/') {
    event.preventDefault();
    if (!search) {
      location.href = '/#search';
      return;
    }
    if (finder) finder.open = true;
    search.focus();
    search.select();
  }
});

if (search && location.hash === '#search') {
  history.replaceState(null, '', location.pathname + location.search);
  if (finder) finder.open = true;
  search.focus();
}

// Skills: filter the cards by state and by words, in the page.
const skillSearch = document.getElementById('skill-search');
const skillStates = document.getElementById('skill-states');
if (skillSearch && skillStates) {
  const cards = [...document.querySelectorAll('.skill')];
  const none = document.getElementById('skills-none');
  let state = '';
  const apply = () => {
    const words = skillSearch.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const card of cards) {
      const visible = (!state || card.dataset.state === state) && words.every(word => card.dataset.search.includes(word));
      card.hidden = !visible;
      if (visible) shown++;
    }
    if (none) none.hidden = shown > 0 || cards.length === 0;
  };
  skillStates.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    state = button.dataset.state;
    for (const other of skillStates.querySelectorAll('button')) other.classList.toggle('on', other === button);
    apply();
  });
  skillSearch.addEventListener('input', apply);
}

// Forms that start slow work: the button says what is happening and cannot be pressed twice.
for (const form of document.querySelectorAll('form.wait')) {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button.primary, button.secondary');
    if (!button) return;
    setTimeout(() => { button.disabled = true; }, 0);
    button.textContent = form.dataset.wait || 'Working…';
  });
}

// The skill chat sends with Cmd/Ctrl+Enter.
for (const box of document.querySelectorAll('textarea[name=message]')) {
  box.addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) box.form.requestSubmit();
  });
}

// Copy: the commands beside the button (a review's merge or delete commands).
document.addEventListener('click', async event => {
  const button = event.target.closest('button[data-copy]');
  if (!button) return;
  const text = button.parentElement.querySelector('pre')?.textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'Copied';
  } catch {
    button.textContent = 'Select and copy';
  }
  setTimeout(() => { button.textContent = 'Copy'; }, 2000);
});

// Running clocks: how long an implementation has been going (or waiting), kept current while the page is open.
const clocks = document.querySelectorAll('[data-since]');
if (clocks.length) {
  const tick = () => {
    for (const clock of clocks) {
      const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(clock.dataset.since)) / 60000));
      clock.textContent = minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
    }
  };
  setInterval(tick, 30000);
}
