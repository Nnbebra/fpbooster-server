/* JavaScript/main.js
   - navbar shrink, smooth anchors
   - hero parallax + progressive darkening + bottom blend
   - hydrate stats with retry + responsive toggler safety
*/
(function () {
  "use strict";

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
    navbar.style.height = s > NAV_SHRINK ? '60px' : '72px';
    navbar.classList.toggle('nav-compact', s > NAV_SHRINK);
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
    // use requestAnimationFrame + throttle for smooth updates
    let raf = null;
    function updateHero(scrollPos) {
      const docH = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
      const pos = clamp(scrollPos / docH, 0, 1);
      const pct = clamp(pos * 1.5, 0, 1);

      // update CSS variable used by overlay gradients
      heroBg.style.setProperty('--hero-dim', String(pct));

      // subtle translate up and scale down to feel "farther"
      const translateY = Math.round(-12 * pct);
      const scale = 1 - 0.01 * pct;
      heroBg.style.transform = `translateX(-50%) translateY(${translateY}px) scale(${scale})`;

      // progressively darken/desaturate to highlight foreground
      const contrast = 0.88 - 0.10 * pct;
      const saturate = 0.98 - 0.14 * pct;
      const brightness = 0.48 - 0.18 * pct;
      heroBg.style.filter = `contrast(${contrast}) saturate(${saturate}) brightness(${brightness})`;

      // toggle deeper state for tiny additional polish
      heroBg.classList.toggle('depth-dark', pos > 0.22);
    }

    const onScroll = throttle(function () {
      const scrollPos = window.scrollY || window.pageYOffset;
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => updateHero(scrollPos));
    }, 40);

    window.addEventListener('scroll', onScroll, { passive: true });
    // initialize immediately
    updateHero(window.scrollY || window.pageYOffset);

    // ensure hero updates on resize (document height changes)
    window.addEventListener('resize', function () {
      updateHero(window.scrollY || window.pageYOffset);
    }, { passive: true });
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

  // responsive navbar toggler: visible only on small screens; safety guard so it won't affect desktop
  (function navbarToggler() {
    const toggler = document.querySelector('.navbar-toggler');
    const navRight = document.querySelector('.nav-right');
    if (!toggler || !navRight) return;

    function sync() {
      if (window.innerWidth <= 880) {
        // mobile mode: leave navRight controlled by toggler (CSS hides it by default)
        toggler.setAttribute('aria-hidden', 'false');
      } else {
        // desktop: ensure nav is visible and toggler reset
        navRight.style.display = '';
        toggler.setAttribute('aria-expanded', 'false');
        toggler.setAttribute('aria-hidden', 'true');
      }
    }

    toggler.addEventListener('click', () => {
      if (window.innerWidth > 880) return; // defensive
      const expanded = toggler.getAttribute('aria-expanded') === 'true';
      toggler.setAttribute('aria-expanded', String(!expanded));
      navRight.style.display = navRight.style.display === 'flex' ? 'none' : 'flex';
    });

    window.addEventListener('resize', throttle(sync, 120), { passive: true });
    sync();
  })();

})();
