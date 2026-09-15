(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.remove('pending'); observer.unobserve(entry.target); }
    }), { threshold: .06 });
    document.querySelectorAll('.card-link,.step,.master-photo,.equip,.review,.sec-head,.svc-photo,.faq-item').forEach(el => {
      el.classList.add('reveal');
      if (el.getBoundingClientRect().top > innerHeight) { el.classList.add('pending'); observer.observe(el); }
    });
  }
  document.querySelectorAll('.form-note').forEach(el => { el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite'); });
  const lead = document.querySelector('#leadForm');
  const floating = document.querySelector('.mobile-apply');
  if (lead && floating && 'IntersectionObserver' in window) {
    new IntersectionObserver(entries => {
      floating.style.display = entries[0].isIntersecting ? 'none' : '';
    }, {threshold: .1}).observe(lead);
  }
  const menu = document.querySelector('#navLinks');
  const burger = document.querySelector('.burger');
  if (menu && burger) {
    burger.setAttribute('aria-controls', 'navLinks');
    const update = () => burger.setAttribute('aria-expanded', String(menu.classList.contains('open')));
    new MutationObserver(update).observe(menu, { attributes: true, attributeFilter: ['class'] }); update();
    menu.addEventListener('click', () => menu.classList.remove('open'));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.classList.remove('open'); });
  }
  const modal = document.querySelector('#statusModal');
  if (modal) {
    modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true'); modal.setAttribute('aria-label', 'Статус ремонта');
    let previous;
    new MutationObserver(() => {
      if (modal.classList.contains('open')) { previous = document.activeElement; modal.querySelector('input')?.focus(); }
      else previous?.focus();
    }).observe(modal, {attributes:true,attributeFilter:['class']});
    modal.addEventListener('keydown', e => {
      if(e.key === 'Escape') window.closeStatus();
      if(e.key !== 'Tab') return;
      const items = [...modal.querySelectorAll('button,input,a[href]')].filter(el => !el.disabled && el.offsetParent !== null);
      const first=items[0], last=items[items.length-1];
      if(e.shiftKey && document.activeElement===first){e.preventDefault();last.focus();}
      else if(!e.shiftKey && document.activeElement===last){e.preventDefault();first.focus();}
    });
  }
})();
