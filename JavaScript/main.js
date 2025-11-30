/* JavaScript/main.js - No Parallax Version */
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

})();
