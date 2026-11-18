import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Toaster } from '../components/Toaster';
import { Compass } from '../pages/Compass';
import { Inbox } from '../pages/Inbox';
import { Projects } from '../pages/Projects';
import { Today } from '../pages/Today';
import { Week } from '../pages/Week';
import '../styles/tokens.css';
import '../styles/base.css';
import '../styles/ui.css';
import '../styles/pages-compass.css';

const params = new URLSearchParams(window.location.search);
const page = params.get('page') ?? 'compass';
const theme = params.get('theme');
document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';

const PAGES = { compass: Compass, today: Today, week: Week, inbox: Inbox, projects: Projects } as const;
const Component = PAGES[page as keyof typeof PAGES] ?? Compass;

const container = document.getElementById('root');
if (!container) throw new Error('Нет #root');

createRoot(container).render(
  <StrictMode>
    <div className="pc-preview-shell">
      <Component />
      <Toaster />
    </div>
  </StrictMode>,
);
