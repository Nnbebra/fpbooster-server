/* JavaScript/ui.js
   Responsibilities:
   - Payment method selection widget (reusable)
   - Image fallback handler for remote assets (data-fallback attribute)
   - Lightweight button press micro-interactions
*/

(function () {
  "use strict";

  // Payment method selector (reusable)
  function initPaymentSelector(root) {
    const pmList = document.querySelector('#pm-list');
    const methodInput = document.querySelector('#pay-method-input');
    const continueBtn = document.querySelector('#continue-btn');

    if (!pmList) return;

    pmList.addEventListener('click', (e) => {
      const pm = e.target.closest('.pm-method');
      if (!pm) return;
      pmList.querySelectorAll('.pm-method').forEach(el => el.classList.remove('active'));
      pm.classList.add('active');
      const method = pm.getAttribute('data-method') || 'card';
      if (methodInput) methodInput.value = method;
      if (continueBtn) {
        continueBtn.innerHTML = '<i class="fa-solid fa-circle-arrow-right mr-2"></i> Продолжить';
        continueBtn.dataset.method = method;
      }
    }, { passive: true });

    // Ensure pm-method elements are focusable and keyboard accessible
    pmList.querySelectorAll('.pm-method').forEach((el, i) => {
      el.setAttribute('tabindex', '0');
    });
    pmList.addEventListener('keydown', (e) => {
      const items = Array.from(pmList.querySelectorAll('.pm-method'));
      if (!items.length) return;
      const active = pmList.querySelector('.pm-method.active') || items[0];
      let idx = items.indexOf(active);
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        idx = (idx + 1) % items.length;
        items[idx].focus();
        items[idx].click();
        e.preventDefault();
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        idx = (idx - 1 + items.length) % items.length;
        items[idx].focus();
        items[idx].click();
        e.preventDefault();
      } else if (e.key === 'Enter') {
        active.click();
      }
    });
  }

  // Fallback loader for images with data-fallback attr
  function initImageFallback(root = document) {
    const imgs = root.querySelectorAll('img[data-fallback]');
    imgs.forEach(img => {
      img.addEventListener('error', function onErr() {
        img.removeEventListener('error', onErr);
        const fb = img.getAttribute('data-fallback') || '/static/img/fallback.png';
        if (img.src !== fb) img.src = fb;
      });
    });
  }

  // Micro interaction on pointer down
  function initButtonPress() {
    document.addEventListener('pointerdown', (e) => {
      const btn = e.target.closest('.btn, .btn-cta, .btn-outline, .btn-gradient, .btn-light');
      if (!btn) return;
      btn.style.transform = 'translateY(1px) scale(.997)';
      btn.style.transition = 'transform 80ms ease';
      const up = () => {
        btn.style.transform = '';
        btn.removeEventListener('pointerup', up);
        btn.removeEventListener('pointercancel', up);
      };
      btn.addEventListener('pointerup', up);
      btn.addEventListener('pointercancel', up);
    });
  }

  // Init on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      initPaymentSelector();
      initImageFallback();
      initButtonPress();
    });
  } else {
    initPaymentSelector();
    initImageFallback();
    initButtonPress();
  }

  // Expose lightweight API for manual hydration
  window.FPBooster = window.FPBooster || {};
  window.FPBooster.hydrateStats = function (data) {
    if (!data) return;
    if (data.users && document.getElementById('stat-users')) document.getElementById('stat-users').textContent = data.users;
    if (data.runs && document.getElementById('stat-runs')) document.getElementById('stat-runs').textContent = data.runs;
    if (data.subs && document.getElementById('footer-subs')) document.getElementById('footer-subs').textContent = data.subs;
    if (data.rates && document.getElementById('footer-rates')) document.getElementById('footer-rates').textContent = data.rates;
    if (data.version && document.getElementById('upd-ver')) document.getElementById('upd-ver').textContent = data.version;
    if (data.changelog && document.getElementById('upd-changelog')) document.getElementById('upd-changelog').textContent = data.changelog;
  };

})();
