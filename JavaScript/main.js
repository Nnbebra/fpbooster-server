/* JavaScript/main.js */
(function () {
  "use strict";

  // Navbar background change
  const navbar = document.querySelector('.navbar');
  if (navbar) {
    window.addEventListener('scroll', () => {
      if (window.scrollY > 50) {
        navbar.style.background = 'rgba(5,5,6,0.95)';
        navbar.style.boxShadow = '0 10px 30px rgba(0,0,0,0.5)';
      } else {
        navbar.style.background = 'rgba(5,5,6,0.7)';
        navbar.style.boxShadow = 'none';
      }
    });
  }

  // --- MOBILE MENU ---
  const toggler = document.querySelector('.navbar-toggler');
  const navRight = document.querySelector('.nav-right');
  
  if (toggler && navRight) {
    toggler.addEventListener('click', () => {
      // 1. Меняем атрибут aria-expanded для CSS анимации крестика
      const isExpanded = toggler.getAttribute('aria-expanded') === 'true';
      toggler.setAttribute('aria-expanded', !isExpanded);

      // 2. Логика показа меню
      if (!isExpanded) {
        // OPEN
        navRight.style.display = 'flex';
        // Небольшая задержка для анимации появления (если захочешь добавить opacity)
        requestAnimationFrame(() => {
            navRight.style.position = 'absolute';
            navRight.style.top = '76px';
            navRight.style.left = '0';
            navRight.style.right = '0';
            navRight.style.background = 'rgba(5, 5, 6, 0.95)'; // Glassmorphism
            navRight.style.backdropFilter = 'blur(20px)';
            navRight.style.flexDirection = 'column';
            navRight.style.padding = '30px';
            navRight.style.borderBottom = '1px solid rgba(255,255,255,0.1)';
            navRight.style.boxShadow = '0 20px 40px rgba(0,0,0,0.5)';
            navRight.style.zIndex = '1000';
            navRight.style.gap = '20px';
        });
      } else {
        // CLOSE
        navRight.style.display = 'none';
        navRight.style = ''; // Сброс инлайн стилей
      }
    });
  }
  
  // Hydrate Stats
  (function hydrateStats() {
    const url = '/api/update';
    fetch(url).then(r => r.json()).then(data => {
       if(!data) return;
       if(data.stats && data.stats.users) {
          const el = document.getElementById('stat-users');
          if(el) el.textContent = data.stats.users;
       }
    }).catch(() => {});
  })();

  // Scroll Reveal
  function initScrollReveal() {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('active');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.1 });

    document.querySelectorAll('.card, .stats-row, .section-title, .video-wrap').forEach(el => {
      el.classList.add('reveal-section');
      observer.observe(el);
    });
  }
  
  initScrollReveal();

})();
