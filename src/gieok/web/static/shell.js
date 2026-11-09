const filters = document.querySelector('.work-filters');
if (filters) {
  if (window.htmx) filters.classList.add('filters-enhanced');
  const desktop = matchMedia('(min-width: 761px)');
  const update = () => { filters.open = desktop.matches; };
  update();
  desktop.addEventListener('change', update);
}
