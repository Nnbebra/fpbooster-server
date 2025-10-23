/* JavaScript/main.js
   - navbar shrink, smooth anchors
   - hero parallax + progressive darkening + bottom blend
   - hydrate stats with retry + default replacements
   - small UX helpers
*/
(function () {
  "use strict";

  // helpers
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

  // navbar compact on scroll
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

  // smooth anchor scrolling
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

  // hero parallax + progressive darkening and distant feel
  const heroBg = document.querySelector('.hero-bg');
  if (heroBg) {
    const onHeroScroll = throttle(function () {
      const docH = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
      const pos = clamp((window.scrollY || 0) / docH, 0, 1);
      const pct = clamp(pos * 1.5, 0, 1);

      // progressively increase dim variable for CSS overlay
      heroBg.style.setProperty('--hero-dim', String(pct));

      // subtle translate up and scale down to feel "farther"
      const translateY = Math.round(-12 * pct);
      const scale = 1 - 0.008 * pct;
      heroBg.style.transform = `translateX(-50%) translateY(${translateY}px) scale(${scale})`;

      // gently darken and desaturate with scroll to highlight foreground
      const contrast = 0.88 - 0.10 * pct;
      const saturate = 0.98 - 0.10 * pct;
      const brightness = 0.48 - 0.16 * pct;
      heroBg.style.filter = `contrast(${contrast}) saturate(${saturate}) brightness(${brightness})`;

      // add state near deeper scroll
      heroBg.classList.toggle('depth-dark', pos > 0.22);
    }, 60);
    window.addEventListener('scroll', onHeroScroll, { passive: true });
    onHeroScroll();
  }

  // focus ring for keyboard users
  (function manageFocusRing() {
    function handleFirstTab(e) {
      if (e.key === 'Tab') {
        document.body.classList.add('user-is-tabbing');
        window.removeEventListener('keydown', handleFirstTab);
      }
    }
    window.addEventListener('keydown', handleFirstTab);
  })();

  // default stat replacements (ensure requested numbers)
  (function applyStatDefaults() {
    const replacements = [
      { ids: ['stat-runs', 'stat-runs-hero'], from: '5752', to: '2393' },
      { ids: ['days-open'], from: '1111', to: '68' },
      { ids: ['footer-rates'], from: '1752', to: '844' }
    ];
    replacements.forEach(r => {
      r.ids.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const text = (el.textContent || '').trim();
        if (!text || text === r.from || text === '') el.textContent = r.to;
      });
    });
  })();

  // hydrate update/stats from API (retry once)
  (function hydrateStats() {
    const url = '/api/update';
    const apply = (data) => {
      if (!data) return;
      if (data.version && document.getElementById('upd-ver')) document.getElementById('upd-ver').textContent = data.version;
      if (data.changelog && document.getElementById('upd-changelog')) document.getElementById('upd-changelog').textContent = data.changelog;
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

  // navbar toggler (responsive)
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
