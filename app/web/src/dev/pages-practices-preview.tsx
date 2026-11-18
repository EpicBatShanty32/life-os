import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Practices } from '../pages/Practices';
import '../styles/tokens.css';
import '../styles/base.css';
import '../styles/ui.css';
import '../styles/pages-compass.css';
import '../styles/practices.css';

/* Страница предпросмотра: `?page=practices&theme=dark`.
   Для скриншотов и проверки плеера без кликов:
     ?auto=<номер практики в каталоге>&at=<мс от старта>
   Время замораживается, поэтому кадр детерминированный. */

const params = new URLSearchParams(window.location.search);
const theme = params.get('theme');
document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';

/* Просим у браузера «уменьшенное движение» без системных настроек. */
if (params.get('rm') === '1') {
  const original = window.matchMedia.bind(window);
  window.matchMedia = ((query: string) => {
    if (!query.includes('prefers-reduced-motion')) return original(query);
    return {
      matches: true,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    } as MediaQueryList;
  }) as typeof window.matchMedia;
}

function autoStart() {
  const raw = params.get('auto');
  if (raw === null) return;
  const index = Number(raw) || 0;
  const at = Number(params.get('at') ?? 0);

  window.setTimeout(() => {
    const openButton = document.querySelectorAll<HTMLElement>('.pp-row .ui-btn')[index];
    openButton?.click();

    if (params.get('open') === '1') return;

    window.setTimeout(() => {
      const startButton = Array.from(document.querySelectorAll<HTMLElement>('.pp-foot .ui-btn')).find(
        (button) => button.textContent?.trim() === 'Начать',
      );
      const startedAt = performance.now();
      startButton?.click();

      if (Number.isFinite(at) && at > 0) {
        performance.now = () => startedAt + at;
      }
    }, 200);
  }, 300);
}

const container = document.getElementById('root');
if (!container) throw new Error('Нет #root');

createRoot(container).render(
  <StrictMode>
    <div className="pc-preview-shell">
      <Practices />
    </div>
  </StrictMode>,
);

autoStart();
