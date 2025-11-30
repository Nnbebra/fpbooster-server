/* JavaScript/main.js - New Features */
(function () {
  "use strict";

  // 1. --- PARALLAX CONTENT (Вместо фона) ---
  // Двигаем контент, а не фон, для стабильности
  const heroContent = document.querySelector('.hero-left-wrap');
  const heroImage = document.querySelector('.visual-card');
  
  if (heroContent && heroImage) {
    window.addEventListener('scroll', () => {
      const scrollPos = window.scrollY;
      if (window.innerWidth > 1024) { // Только на ПК
        // Текст уезжает чуть быстрее (0.2), Картинка медленнее (0.1) -> Эффект глубины
        heroContent.style.transform = `translateY(${scrollPos * 0.2}px)`;
        heroImage.style.transform = `translateY(${scrollPos * 0.1}px) rotateY(-5deg) rotateX(2deg)`;
      }
    }, { passive: true });
  }

  // 2. --- SCROLL REVEAL (Появление блоков) ---
  function initScrollReveal() {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('active');
          observer.unobserve(entry.target); // Показываем только один раз
        }
      });
    }, { threshold: 0.1 }); // Срабатывает, когда 10% блока видно

    // Ищем все секции и карточки, которые нужно анимировать
    const elementsToReveal = document.querySelectorAll('.card, .stats-row, .section-title, .video-wrap');
    elementsToReveal.forEach(el => {
      el.classList.add('reveal-section'); // Добавляем класс стиля
      observer.observe(el);
    });
  }

  // 3. --- MAGNETIC BUTTONS (Магнитные кнопки) ---
  function initMagneticButtons() {
    if (window.innerWidth <= 1024) return; // Отключаем на мобильных

    const buttons = document.querySelectorAll('.btn-cta, .btn-outline, .nav-link');
    
    buttons.forEach(btn => {
      btn.addEventListener('mousemove', (e) => {
        const rect = btn.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        
        // Вычисляем смещение от центра
        const centerX = rect.width / 2;
        const centerY = rect.height / 2;
        
        const moveX = (x - centerX) * 0.2; // Сила магнита X
        const moveY = (y - centerY) * 0.2; // Сила магнита Y
        
        btn.style.transform = `translate(${moveX}px, ${moveY}px)`;
      });

      btn.addEventListener('mouseleave', () => {
        btn.style.transform = 'translate(0px, 0px)'; // Возврат на место
      });
    });
  }

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

  // Mobile Menu
  const toggler = document.querySelector('.navbar-toggler');
  const navRight = document.querySelector('.nav-right');
  if (toggler && navRight) {
    toggler.addEventListener('click', () => {
      const isVisible = navRight.style.display === 'flex';
      navRight.style.display = isVisible ? 'none' : 'flex';
      if (!isVisible) {
        navRight.style.position = 'absolute';
        navRight.style.top = '76px';
        navRight.style.left = '0';
        navRight.style.right = '0';
        navRight.style.background = '#050405';
        navRight.style.flexDirection = 'column';
        navRight.style.padding = '20px';
        navRight.style.borderBottom = '1px solid rgba(255,255,255,0.1)';
        navRight.style.zIndex = '1000';
      } else {
        navRight.style = '';
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

  // INIT EVERYTHING
  initScrollReveal();
  initMagneticButtons();

})();
