/* JavaScript/ui.js
   - payment selector, image fallback, hitbox/role improvements, button micro-interactions
   - video placeholder/text adjustments
*/
(function () {
  "use strict";

  // --- Payment method selector (generic)
  function initPaymentSelector() {
    const pmList = document.getElementById('pm-list');
    if (!pmList) return;
    const methodInput = document.getElementById('pay-method-input');
    const continueBtn = document.getElementById('continue-btn');

    pmList.addEventListener('click', (e) => {
      const pm = e.target.closest('.pm-method');
      if (!pm) return;
      pmList.querySelectorAll('.pm-method').forEach(el => el.classList.remove('active'));
      pm.classList.add('active');
      const method = pm.getAttribute('data-method') || 'card';
      if (methodInput) methodInput.value = method;
      if (continueBtn) {
        continueBtn.dataset.method = method;
      }
    }, { passive: true });

    // keyboard accessibility
    pmList.querySelectorAll('.pm-method').forEach(el => el.setAttribute('tabindex', '0'));
    pmList.addEventListener('keydown', (e) => {
      const items = Array.from(pmList.querySelectorAll('.pm-method'));
      if (!items.length) return;
      const active = pmList.querySelector('.pm-method.active') || items[0];
      let idx = items.indexOf(active);
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
        idx = (idx + 1) % items.length; items[idx].focus(); items[idx].click(); e.preventDefault();
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
        idx = (idx - 1 + items.length) % items.length; items[idx].focus(); items[idx].click(); e.preventDefault();
      } else if (e.key === 'Enter') {
        active.click();
      }
    });
  }

  // --- image fallback for elements with data-fallback
  function initImageFallback(root = document) {
    const imgs = root.querySelectorAll('img[data-fallback]');
    imgs.forEach(img => {
      img.addEventListener('error', function onErr() {
        img.removeEventListener('error', onErr);
        const fb = img.getAttribute('data-fallback') || '/static/Ui.png';
        if (img.src !== fb) img.src = fb;
      });
    });
  }

  // --- micro-interaction on pointer down to simulate press
  function initButtonPress() {
    document.addEventListener('pointerdown', (e) => {
      const btn = e.target.closest('.btn, .btn-cta, .btn-outline, .btn-gradient, .nav-link, .pm-method');
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

  // --- add roles and enlarge hitboxes for subtle controls
  function enhanceHitboxes() {
    const selectors = ['.nav-link', '.pm-method', '.btn-outline', '.btn-cta', '.btn-gradient'];
    selectors.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        // ensure accessible
        if (!el.getAttribute('role')) el.setAttribute('role', 'button');
        if (!el.getAttribute('tabindex')) el.setAttribute('tabindex', '0');
        // add a utility padding class (visual unchanged if CSS handles appearance)
        el.classList.add('interactive-hitbox');
      });
    });
  }

  // --- video placeholder replacement & text tweak
  function prepareVideoPlaceholder() {
    // change descriptive text if present
    document.querySelectorAll('p').forEach(p => {
      const txt = (p.textContent || '').trim().toLowerCase();
      if (txt.includes('геймплей') || txt.includes('gameplay')) {
        p.textContent = 'Ниже вы можете посмотреть небольшой обзор клиента.';
      }
    });
    // replace iframe with coming soon placeholder if needed
    document.querySelectorAll('.video-wrap, .Home_video__ZcoNm, .video').forEach(container => {
      if (!container) return;
      const iframe = container.querySelector('iframe');
      if (iframe) {
        // remove iframe and add placeholder
        iframe.remove();
        const placeholder = document.createElement('div');
        placeholder.className = 'video-coming-soon';
        placeholder.setAttribute('aria-live', 'polite');
        placeholder.style.cssText = 'display:flex;align-items:center;justify-content:center;padding:40px 16px;background:linear-gradient(180deg, rgba(255,255,255,0.02), transparent);border-radius:12px;color:var(--muted);font-weight:700;';
        placeholder.textContent = 'coming soon';
        container.appendChild(placeholder);
      } else {
        // if no iframe but text is different, ensure placeholder exists
        if (!container.querySelector('.video-coming-soon')) {
          const placeholder = document.createElement('div');
          placeholder.className = 'video-coming-soon';
          placeholder.setAttribute('aria-live', 'polite');
          placeholder.style.cssText = 'display:flex;align-items:center;justify-content:center;padding:40px 16px;background:linear-gradient(180deg, rgba(255,255,255,0.02), transparent);border-radius:12px;color:var(--muted);font-weight:700;';
          placeholder.textContent = 'coming soon';
          container.appendChild(placeholder);
        }
      }
    });
  }

  // --- init on DOM ready
  function init() {
    initPaymentSelector();
    initImageFallback();
    initButtonPress();
    enhanceHitboxes();
    prepareVideoPlaceholder();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // --- expose small API
  window.FPBooster = window.FPBooster || {};
  window.FPBooster.hydrateStats = function (data) {
    if (!data) return;
    if (data.users && document.getElementById('stat-users')) document.getElementById('stat-users').textContent = data.users;
    if (data.runs && document.getElementById('stat-runs')) document.getElementById('stat-runs').textContent = data.runs;
    if (data.runs && document.getElementById('stat-runs-hero')) document.getElementById('stat-runs-hero').textContent = data.runs;
    if (data.subs && document.getElementById('footer-subs')) document.getElementById('footer-subs').textContent = data.subs;
    if (data.rates && document.getElementById('footer-rates')) document.getElementById('footer-rates').textContent = data.rates;
    if (data.days && document.getElementById('days-open')) document.getElementById('days-open').textContent = data.days;
    if (data.version && document.getElementById('upd-ver')) document.getElementById('upd-ver').textContent = data.version;
    if (data.changelog && document.getElementById('upd-changelog')) document.getElementById('upd-changelog').textContent = data.changelog;
  };

})();
