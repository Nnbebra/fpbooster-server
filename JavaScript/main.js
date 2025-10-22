/* JavaScript/main.js
   Responsibilities:
   - Navbar shrink on scroll
   - Smooth anchor scrolling for local anchors
   - Lightweight hero image parallax/visibility polishing
   - Hydrate update stats (retry logic)
*/

(function () {
  "use strict";

  const navbar = document.querySelector('.navbar');
  const NAV_SHRINK = 80;

  function onScrollNav() {
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

  // Smooth anchor scrolling
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

  // Hero parallax
  const heroBg = document.querySelector('.hero-bg');
  if (heroBg) {
    let ticking = false;
    function heroParallax() {
      const y = window.scrollY || window.pageYOffset;
      const t = Math.min(1, y / 800);
      heroBg.style.filter = `contrast(${0.92 - t * 0.06}) saturate(${1.04 - t * 0.06}) brightness(${0.56 - t * 0.08})`;
      heroBg.style.transform = `translateX(-50%) translateY(${Math.round(y * 0.08)}px)`;
      ticking = false;
    }
    window.addEventListener('scroll', function () {
      if (!ticking) {
        ticking = true;
        requestAnimationFrame(heroParallax);
      }
    }, { passive: true });
  }

  // Hydrate small interactive pieces: update stats fetch (retry once)
  (function hydrateStats() {
    const url = '/api/update';
    const apply = (data) => {
      if (!data) return;
      if (data.version && document.getElementById('upd-ver')) document.getElementById('upd-ver').textContent = data.version;
      if (data.changelog && document.getElementById('upd-changelog')) document.getElementById('upd-changelog').textContent = data.changelog;
      if (data.stats){
        if(data.stats.users) document.getElementById('stat-users').textContent = data.stats.users;
        if(data.stats.runs) document.getElementById('stat-runs').textContent = data.stats.runs;
        if(data.stats.subs && document.getElementById('footer-subs')) document.getElementById('footer-subs').textContent = data.stats.subs;
        if(data.stats.rates && document.getElementById('footer-rates')) document.getElementById('footer-rates').textContent = data.stats.rates;
      }
    };
    fetch(url, { cache: 'no-store' }).then(r => {
      if (!r.ok) throw new Error('no update');
      return r.json();
    }).then(apply).catch(() => {
      setTimeout(() => {
        fetch(url, { cache: 'no-store' }).then(r => r.ok && r.json()).then(apply).catch(() => { /* silent */ });
      }, 1200);
    });
  })();

  // Keyboard focus ring helper
  (function manageFocusRing() {
    function handleFirstTab(e) {
      if (e.key === 'Tab') {
        document.body.classList.add('user-is-tabbing');
        window.removeEventListener('keydown', handleFirstTab);
      }
    }
    window.addEventListener('keydown', handleFirstTab);
  })();

})();
