import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/tokens.css';
import './styles/base.css';
import './styles/layout.css';
import './styles/pages.css';

const container = document.getElementById('root');

if (!container) {
  throw new Error('Не найден контейнер #root');
}

// PWA: service worker только в собранном приложении — в dev он мешает HMR.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch(() => undefined);
  });
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
