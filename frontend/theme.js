// ═══════════════ THEME SWITCHER ═══════════════
// iOS‑style 3‑position toggle: dark | system | light
// Saves to localStorage, respects prefers-color-scheme

(function(){
  const KEY = 'hermes-theme';
  const MODES = ['dark', 'system', 'light'];

  function getMode(){
    return localStorage.getItem(KEY) || 'system';
  }

  function getEffective(){
    const mode = getMode();
    if (mode === 'system'){
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    return mode;
  }

  function applyTheme(ef){
    document.documentElement.setAttribute('data-theme', ef);
    // Update chart themes if LightweightCharts is loaded
    if (typeof window.updateChartTheme === 'function'){
      window.updateChartTheme(ef);
    }
  }

  function updateSlider(pos){
    const slider = document.querySelector('.theme-slider');
    if (slider){
      const idx = MODES.indexOf(pos);
      slider.style.left = (idx * 34 + 2) + 'px';
    }
    // Update active button
    document.querySelectorAll('.theme-option').forEach(b => {
      b.classList.toggle('active', b.dataset.themeOpt === pos);
    });
  }

  function setTheme(mode){
    if (!MODES.includes(mode)) return;
    localStorage.setItem(KEY, mode);
    applyTheme(getEffective());
    updateSlider(mode);
  }

  // ═══ PUBLIC: inject toggle into a container ═══
  window.injectThemeToggle = function(containerSelector){
    const container = document.querySelector(containerSelector);
    if (!container) return;

    const html = `
      <div class="theme-toggle">
        <div class="theme-slider"></div>
        <button class="theme-option" data-theme-opt="dark" title="Тёмная">🌙</button>
        <button class="theme-option" data-theme-opt="system" title="Как в системе">💻</button>
        <button class="theme-option" data-theme-opt="light" title="Светлая">☀️</button>
      </div>`;

    container.insertAdjacentHTML('beforeend', html);

    // Click handler
    container.querySelector('.theme-toggle').addEventListener('click', function(e){
      const btn = e.target.closest('.theme-option');
      if (btn) setTheme(btn.dataset.themeOpt);
    });

    // Init slider position
    updateSlider(getMode());
  };

  // Init on load
  applyTheme(getEffective());

  // Listen for system changes when in 'system' mode
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(){
    if (getMode() === 'system') applyTheme(getEffective());
  });
})();
