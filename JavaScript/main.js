// main.js
// 1) Hero background scroll depth effect + blending
// 2) Ensure interactive hitboxes respond well (small UX helpers)
// 3) Provide defaults for stats if API doesn't override
(function () {
  "use strict";

  // debounce helper
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

  // Hero depth effect
  const heroBg = document.querySelector('.hero-bg');
  if (heroBg) {
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
    const onScroll = throttle(function () {
      const docH = document.documentElement.scrollHeight - window.innerHeight;
      const pos = docH > 0 ? window.scrollY / docH : 0;
      // add / remove class at threshold while also applying smooth transform
      if (pos > 0.18) heroBg.classList.add('depth-dark'); else heroBg.classList.remove('depth-dark');

      const pct = clamp(pos * 1.2, 0, 1);
      heroBg.style.filter = `contrast(${1 - 0.12 * pct}) saturate(${1 - 0.04 * pct}) brightness(${1 - 0.42 * pct})`;
      heroBg.style.transform = `translateY(${ -6 * pct }px) scale(${1 + 0.02 * pct})`;
      heroBg.style.opacity = `${1 - 0.06 * pct}`;
    }, 80);

    window.addEventListener('scroll', onScroll, { passive: true });
    // initial call
    onScroll();
  }

  // Small UX: expand clickable area on small controls (adds aria for screen readers)
  document.querySelectorAll('.pm-method, .nav-link, .btn-outline, .btn-cta, .btn-gradient').forEach(el => {
    el.setAttribute('tabindex', el.getAttribute('tabindex') || '0');
    if (!el.hasAttribute('role')) el.setAttribute('role', 'button');
  });

  // Set conservative default stats to match requested values (will be overwritten by API if available)
  const defaultValues = {
    statRuns: '2393',
    footerRates: '844',
    daysOpen: '68'
  };

  (function applyDefaults() {
    const elRuns = document.getElementById('stat-runs');
    const elRates = document.getElementById('footer-rates');
    const elDays = document.getElementById('days-open');

    if (elRuns && (!elRuns.textContent || elRuns.textContent.trim() === '')) elRuns.textContent = defaultValues.statRuns;
    if (elRuns && elRuns.textContent && elRuns.textContent.trim() === '5752') elRuns.textContent = defaultValues.statRuns; // replace older default if present

    if (elRates && (!elRates.textContent || elRates.textContent.trim() === '')) elRates.textContent = defaultValues.footerRates;
    if (elRates && elRates.textContent && elRates.textContent.trim() === '1752') elRates.textContent = defaultValues.footerRates; // safe replace

    if (elDays && (!elDays.textContent || elDays.textContent.trim() === '')) elDays.textContent = defaultValues.daysOpen;
    if (elDays && elDays.textContent && elDays.textContent.trim() === '1111') elDays.textContent = defaultValues.daysOpen; // safe replace
  })();

  // Payment modal small behavior (if present on page)
  (function pmBehavior() {
    const pmList = document.getElementById('pm-list');
    const payMethodInput = document.getElementById('pay-method-input');
    const continueBtn = document.getElementById('continue-btn');
    if (!pmList) return;

    pmList.addEventListener('click', (e) => {
      const pm = e.target.closest('.pm-method');
      if (!pm) return;
      pmList.querySelectorAll('.pm-method').forEach(el => el.classList.remove('active'));
      pm.classList.add('active');
      const method = pm.getAttribute('data-method') || 'card';
      if (payMethodInput) payMethodInput.value = method;
      if (continueBtn) continueBtn.innerHTML = '<i class="fa-solid fa-circle-arrow-right mr-2"></i> Продолжить';
    });

    // fallback images handling
    pmList.querySelectorAll('img').forEach(img => {
      img.addEventListener('error', function () {
        if (this.dataset._fallbackApplied) return;
        this.dataset._fallbackApplied = '1';
        this.src = '/static/img/payments/fallback.png';
      });
    });
  })();

})();
