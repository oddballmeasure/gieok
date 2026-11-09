const filters = document.querySelector('.work-filters');
if (filters) {
  if (window.htmx) filters.classList.add('filters-enhanced');
  const desktop = matchMedia('(min-width: 761px)');
  const update = () => { filters.open = desktop.matches; };
  update();
  desktop.addEventListener('change', update);
}

// Confirm destructive actions before sending their POST forms.
document.addEventListener('submit', (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
