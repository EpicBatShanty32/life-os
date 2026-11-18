/**
 * Временный предпросмотр «Личное», «Итоги», «Аналитика» для живого smoke.
 * Не входит в сборку приложения: отдельная html-точка входа.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Toaster } from '../components/Toaster';
import { Analytics } from '../pages/Analytics';
import { Personal } from '../pages/Personal';
import { Review } from '../pages/Review';
import '../styles/tokens.css';
import '../styles/base.css';
import '../styles/ui.css';
import '../styles/layout.css';

const params = new URLSearchParams(window.location.search);
const theme = params.get('theme') === 'dark' ? 'dark' : 'light';
const page = params.get('page');
document.documentElement.dataset.theme = theme;

const shell = document.getElementById('root');
if (!shell) throw new Error('Нет #root');
shell.style.maxWidth = '1180px';
shell.style.margin = '0 auto';
shell.style.minHeight = '0';
shell.style.padding = '8px 32px 48px';
if (window.innerWidth < 760) shell.style.padding = '8px 16px 48px';

createRoot(shell).render(
  <StrictMode>
    {page === 'review' ? <Review /> : page === 'analytics' ? <Analytics /> : <Personal />}
    <Toaster />
  </StrictMode>,
);
