/* ============================================================
   fantasy.js — ambient alchemy effects shared by every page.
   • floating golden / crimson embers drifting upward
   • a transmutation-circle "burst" wherever you click
   • click the brand logo → a full-screen alchemy flash (easter egg)
   Purely cosmetic, pointer-events disabled, honours reduced-motion.
   ============================================================ */
(function () {
  if (window.__fantasyLoaded) return;
  window.__fantasyLoaded = true;

  const reduce = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- living backdrop: drifting glow clouds ---------- */
  function spawnBackdrop() {
    const bg = document.createElement('div');
    bg.className = 'fx-bg';
    bg.innerHTML = '<span class="fx-glow g1"></span><span class="fx-glow g2"></span><span class="fx-glow g3"></span>';
    document.body.appendChild(bg);
  }

  /* ---------- floating embers ---------- */
  function spawnEmbers() {
    const field = document.createElement('div');
    field.className = 'fx-embers';
    document.body.appendChild(field);
    const N = 16;
    for (let i = 0; i < N; i++) {
      const e = document.createElement('span');
      e.className = 'fx-ember' + (i % 2 ? ' crimson' : '');
      const dur = 9 + Math.random() * 11;
      const size = 2 + Math.random() * 3.5;
      e.style.left = (Math.random() * 100) + 'vw';
      e.style.width = e.style.height = size + 'px';
      e.style.animationDuration = dur + 's';
      e.style.animationDelay = (-Math.random() * dur) + 's';
      e.style.setProperty('--drift', (Math.random() * 80 - 40) + 'px');
      field.appendChild(e);
    }
  }

  /* ---------- transmutation burst ---------- */
  function burst(x, y, big) {
    const b = document.createElement('div');
    b.className = 'fx-burst' + (big ? ' big' : '');
    b.style.left = x + 'px';
    b.style.top = y + 'px';
    document.body.appendChild(b);
    b.addEventListener('animationend', () => b.remove());
    setTimeout(() => b.remove(), 1300);
  }

  /* ---------- transmutation spark trail ---------- */
  function sparks(x, y, n) {
    const count = n || (7 + Math.floor(Math.random() * 5));
    for (let i = 0; i < count; i++) {
      const s = document.createElement('div');
      s.className = 'fx-spark';
      // radiate outward at an even angle + jitter, random distance
      const ang = (i / count) * Math.PI * 2 + Math.random() * 0.6;
      const dist = 26 + Math.random() * 46;
      s.style.left = x + 'px';
      s.style.top = y + 'px';
      s.style.setProperty('--sp-x', Math.cos(ang) * dist + 'px');
      s.style.setProperty('--sp-y', Math.sin(ang) * dist + 'px');
      s.style.setProperty('--sp-dur', (0.85 + Math.random() * 0.6).toFixed(2) + 's');
      document.body.appendChild(s);
      s.addEventListener('animationend', () => s.remove());
      setTimeout(() => s.remove(), 1800);
    }
  }

  /* ---------- page-transition loading screen (the two dogs) ---------- */
  let navOverlay = null, navGoing = false;
  function buildNav() {
    if (navOverlay) return navOverlay;
    const o = document.createElement('div');
    o.className = 'fx-nav';
    o.innerHTML =
      '<div class="fx-nav__scene"></div>' +
      '<div class="fx-nav__veil"></div>' +
      '<div class="fx-nav__stage">' +
        '<div class="fx-dogs">' +
          '<div class="fx-dogcol"><img class="fx-dog d1" src="/static/dog1.png" alt=""><span class="fx-shadow s1"></span></div>' +
          '<div class="fx-dogcol"><img class="fx-dog d2" src="/static/dog2.png" alt=""><span class="fx-shadow s2"></span></div>' +
        '</div>' +
        '<div class="fx-nav__title" id="fxNavTitle">Opening</div>' +
        '<div class="fx-nav__sub" id="fxNavSub">Loading</div>' +
        '<div class="fx-nav__bar"><i></i></div>' +
      '</div>';
    document.body.appendChild(o);
    navOverlay = o;
    return o;
  }
  function navTransition(url, title, sub) {
    if (navGoing) return;
    navGoing = true;
    const o = buildNav();
    o.querySelector('#fxNavTitle').textContent = title || 'Opening';
    o.querySelector('#fxNavSub').textContent = sub || 'Loading';
    requestAnimationFrame(() => o.classList.add('show'));
    setTimeout(() => { window.location.href = url; }, 1300);
  }

  function wireNav() {
    // Intercept internal navigation (bubble phase, so handlers that call
    // stopPropagation — e.g. the job-card Active toggle — opt out cleanly).
    document.addEventListener('click', (ev) => {
      if (ev.defaultPrevented) return;
      if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      const a = ev.target.closest && ev.target.closest('a[href]');
      if (!a || a.target === '_blank' || a.hasAttribute('data-no-transition')) return;
      const href = a.getAttribute('href');
      if (!href || href[0] !== '/' || href.startsWith('/resume/') || href.startsWith('//')) return;
      const dest = new URL(href, location.href);
      if (dest.pathname === location.pathname && dest.search === location.search) return;

      let title = 'Opening', sub = 'Loading';
      const card = a.closest('.job-card');
      const nm = card && card.querySelector('.job-name');
      if (nm) { title = nm.textContent.trim(); sub = 'Opening candidate pipeline'; }
      else if (href.startsWith('/job/')) { sub = 'Opening candidate pipeline'; }
      else if (href.startsWith('/tracking')) { title = 'Status Tracking'; sub = 'Gathering candidates'; }
      else if (href === '/') { title = 'Postings'; sub = 'Returning to the board'; }
      else if (a.textContent.trim()) { title = a.textContent.trim().replace(/^[^\p{L}\p{N}]+/u, ''); }

      ev.preventDefault();
      navTransition(href, title, sub);
    }, false);
  }

  function wire() {
    if (reduce) return;                 // reduced-motion: skip all effects + transitions
    // warm the cache so the transition shows instantly
    ['/static/navigate.png', '/static/dog1.png', '/static/dog2.png'].forEach((s) => { const i = new Image(); i.src = s; });
    wireNav();
    spawnBackdrop();
    spawnEmbers();

    // a small burst + spark trail follows every click (behind the cursor, never blocks it)
    document.addEventListener('click', (ev) => {
      burst(ev.clientX, ev.clientY, false);
      sparks(ev.clientX, ev.clientY);
    }, true);

    // easter egg: tap the logo for a grand central transmutation flash
    const logo = document.querySelector('.brand');
    if (logo) {
      logo.style.cursor = 'pointer';
      logo.title = 'Transmute!';
      logo.addEventListener('click', () => {
        burst(window.innerWidth / 2, window.innerHeight / 2, true);
        sparks(window.innerWidth / 2, window.innerHeight / 2, 28);
        document.body.classList.add('fx-flash');
        setTimeout(() => document.body.classList.remove('fx-flash'), 700);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
