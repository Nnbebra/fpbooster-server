/* JavaScript/main.js - Обновленный параллакс */
(function () {
  "use strict";

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  // Параллакс фона
  const heroBg = document.querySelector('.hero-bg');
  if (heroBg) {
    let raf = null;

    function updateHero() {
      const scrollPos = window.scrollY || window.pageYOffset;
      // Двигаем фон медленнее скролла (эффект глубины)
      // Мы двигаем его только по Y
      const translateY = scrollPos * 0.4; 
      
      // Применяем движение.
      // Важно: mask-image остается на месте, двигается сам блок, эффект будет отличный
      heroBg.style.transform = `translate3d(0, ${translateY}px, 0)`;
    }

    // Слушаем скролл
    window.addEventListener('scroll', () => {
      if (!raf) {
        raf = requestAnimationFrame(() => {
          updateHero();
          raf = null;
        });
      }
    }, { passive: true });
  }

  // Navbar меняет цвет при скролле
  const navbar = document.querySelector('.navbar');
  window.addEventListener('scroll', () => {
    if (window.scrollY > 50) {
      navbar.style.background = 'rgba(5,5,6,0.95)';
      navbar.style.boxShadow = '0 10px 30px rgba(0,0,0,0.5)';
    } else {
      navbar.style.background = 'rgba(5,5,6,0.5)';
      navbar.style.boxShadow = 'none';
    }
  });

  // Мобильное меню
  const toggler = document.querySelector('.navbar-toggler');
  const navRight = document.querySelector('.nav-right');
  if (toggler && navRight) {
    toggler.addEventListener('click', () => {
      const isVisible = navRight.style.display === 'flex';
      navRight.style.display = isVisible ? 'none' : 'flex';
      
      // Стили для мобильного меню
      if (!isVisible) {
        navRight.style.position = 'absolute';
        navRight.style.top = '72px';
        navRight.style.left = '0';
        navRight.style.right = '0';
        navRight.style.background = '#050405';
        navRight.style.flexDirection = 'column';
        navRight.style.padding = '20px';
        navRight.style.borderBottom = '1px solid rgba(255,255,255,0.1)';
      }
    });
  }
  
  // Инициализация статистики (твоя старая функция)
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

})();
