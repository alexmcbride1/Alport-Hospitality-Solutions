(() => {
  const menu = document.getElementById('home-mobile-menu');
  if (!menu) return;
  const toggle = menu.querySelector('summary');
  menu.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link) return;
    menu.open = false;
    const href = link.getAttribute('href');
    if (href.startsWith('#')) {
      const section = document.getElementById(href.slice(1));
      if (section) {
        section.setAttribute('tabindex', '-1');
        section.focus({preventScroll: true});
      }
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.open) {
      menu.open = false;
      toggle.focus();
    }
  });
  document.addEventListener('click', event => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  const desktop = window.matchMedia('(min-width: 1001px)');
  desktop.addEventListener('change', () => { if (desktop.matches) menu.open = false; });
})();
