/* JavaScript/main.js
   - navbar shrink, smooth anchors, hero parallax + depth darkening
   - hydrate stats with retry, apply default/replace numbers
   - small UX helpers (focus ring, accessible roles)
*/
(function () {
  "use strict";

  // --- helpers
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  function throttle(fn, wait) {
    let last = 0;
    return function () {
      const now = Date.now();
      if (now - last >= wait) {
        last = now;
        fn.apply(this, arguments);
      }
    };
  }

  // --- navbar compact on scroll
  const navbar = document.querySelector('.navbar');
  const NAV_SHRINK = 80;
  function onScrollNav() {
    if (!navbar) return;
    const s = window.scrollY || window.pageYOffset;
    if (s > NAV_SHRINK) {
      navbar.classList.add('nav-compact');
      navbar.style.height = '60px';
    } else {
      navbar.classList.remove('nav-compact');
      navbar.style.height = '72px';
    }
  }
  window.addEventListener('scroll', onScrollNav, { passive: true });
  onScrollNav();

  // --- smooth anchor scrolling
  document.addEventListener('click', function (e) {
    const a = e.target.closest('a[href^="#"]');
    if (!a) return;
    const id = a.getAttribute('href').slice(1);
    const el = document.getElementById(id);
    if (!el) return;
    e.preventDefault();
    const offset = navbar ? navbar.offsetHeight : 72;
    const top = el.getBoundingClientRect().top + window.scrollY - offset - 18;
    window.scrollTo({ top: top, behavior: 'smooth' });
  });

  // --- hero parallax + depth darkening
  const heroBg = document.querySelector('.hero-bg');
  if (heroBg) {
    const onHeroScroll = throttle(function () {
      const docH = document.documentElement.scrollHeight - window.innerHeight;
      const pos = docH > 0 ? window.scrollY / docH : 0;
      const pct = clamp(pos * 1.2, 0, 1);
      // smooth filter/transform/opacity
      heroBg.style.filter = `contrast(${1 - 0.12 * pct}) saturate(${1 - 0.04 * pct}) brightness(${1 - 0.42 * pct})`;
      heroBg.style.transform = `translateX(-50%) translateY(${Math.round(-6 * pct)}px) scale(${1 + 0.02 * pct})`;
      heroBg.style.opacity = `${1 - 0.06 * pct}`;
      if (pos > 0.18) heroBg.classList.add('depth-dark'); else heroBg.classList.remove('depth-dark');
    }, 70);
    window.addEventListener('scroll', onHeroScroll, { passive: true });
    onHeroScroll();
  }

  // --- small accessibility: focus ring for keyboard users
  (function manageFocusRing() {
    function handleFirstTab(e) {
      if (e.key === 'Tab') {
        document.body.classList.add('user-is-tabbing');
        window.removeEventListener('keydown', handleFirstTab);
      }
    }
    window.addEventListener('keydown', handleFirstTab);
  })();

  // --- apply default stat replacements (replace old values if present)
  (function applyStatDefaults() {
    const replacements = [
      { ids: ['stat-runs', 'stat-runs-hero'], from: '5752', to: '2393' },
      { ids: ['footer-rates'], from: '1752', to: '844' },
      { ids: ['days-open'], from: '1111', to: '68' }
    ];
    replacements.forEach(r => {
      r.ids.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const text = (el.textContent || '').trim();
        if (!text || text === r.from || text === '') el.textContent = r.to;
      });
    });
    // Also replace any stat-runs occurrences in nodes without ids
    document.querySelectorAll('*').forEach(n => {
      if (n.childNodes && n.childNodes.length === 1 && n.childNodes[0].nodeType === Node.TEXT_NODE) {
        const t = n.textContent.trim();
        if (t === '5752') n.textContent = '2393';
        if (t === '1111') n.textContent = '68';
        if (t === '1752') n.textContent = '844';
      }
    });
  })();

  // --- hydrate update/stats from API (retry once)
  (function hydrateStats() {
    const url = '/api/update';
    const apply = (data) => {
      if (!data) return;
      if (data.version && document.getElementById('upd-ver')) document.getElementById('upd-ver').textContent = data.version;
      if (data.changelog && document.getElementById('upd-changelog')) document.getElementById('upd-changelog').textContent = data.changelog;
      // check nested stats object
      const s = data.stats || {};
      if (s.users && document.getElementById('stat-users')) document.getElementById('stat-users').textContent = s.users;
      if (s.runs && document.getElementById('stat-runs')) document.getElementById('stat-runs').textContent = s.runs;
      if (s.runs && document.getElementById('stat-runs-hero')) document.getElementById('stat-runs-hero').textContent = s.runs;
      if (s.subs && document.getElementById('footer-subs')) document.getElementById('footer-subs').textContent = s.subs;
      if (s.rates && document.getElementById('footer-rates')) document.getElementById('footer-rates').textContent = s.rates;
      if (s.days && document.getElementById('days-open')) document.getElementById('days-open').textContent = s.days;
    };
    fetch(url, { cache: 'no-store' }).then(r => {
      if (!r.ok) throw new Error('no update');
      return r.json();
    }).then(apply).catch(() => {
      setTimeout(() => {
        fetch(url, { cache: 'no-store' }).then(r => r.ok && r.json()).then(apply).catch(() => {});
      }, 1200);
    });
  })();

  // --- navbar toggler (for responsive nav if present)
  (function navbarToggler() {
    const toggler = document.querySelector('.navbar-toggler');
    const navRight = document.querySelector('.nav-right');
    if (!toggler || !navRight) return;
    toggler.addEventListener('click', () => {
      const expanded = toggler.getAttribute('aria-expanded') === 'true';
      toggler.setAttribute('aria-expanded', String(!expanded));
      navRight.style.display = navRight.style.display === 'flex' ? 'none' : 'flex';
    });
  })();

})();
