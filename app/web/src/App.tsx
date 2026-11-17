import { useCallback, useEffect, useRef, useState } from 'react';
import type { ComponentType } from 'react';
import { postQuick, useOverview } from './api/client';
import type { QuickEntryInput } from './api/types';
import { CommandPalette } from './components/CommandPalette';
import { ComingSoon } from './components/ComingSoon';
import { Icon } from './components/Icon';
import { MobileNav } from './components/MobileNav';
import { QuickEntryDialog } from './components/QuickEntryDialog';
import { Sidebar } from './components/Sidebar';
import { Toaster } from './components/Toaster';
import { Topbar } from './components/Topbar';
import { findRoute, navigate, useHashRoute } from './lib/router';
import { STORAGE_KEYS, readFlag, writeFlag } from './lib/storage';
import { useTheme } from './lib/theme';
import { toast } from './lib/toast';
import { Analytics } from './pages/Analytics';
import { Body } from './pages/Body';
import { Courses } from './pages/Courses';
import { Goals } from './pages/Goals';
import { Habits } from './pages/Habits';
import { Journal } from './pages/Journal';
import { Mood } from './pages/Mood';
import { NotFound } from './pages/NotFound';
import { Nutrition } from './pages/Nutrition';
import { Overview } from './pages/Overview';
import { Settings } from './pages/Settings';
import { Sleep } from './pages/Sleep';
import { Today } from './pages/Today';
import { Workouts } from './pages/Workouts';

/**
 * Экраны, которые уже собраны. Новые страницы-модули v4 подключаются
 * сюда отдельным шагом, поэтому маршруты и меню от них не зависят.
 */
const PAGE_COMPONENTS: Readonly<Record<string, ComponentType>> = {
  '/today': Today,
  '/goals': Goals,
  '/habits': Habits,
  '/body': Body,
  '/sleep': Sleep,
  '/workouts': Workouts,
  '/nutrition': Nutrition,
  '/courses': Courses,
  '/journal': Journal,
  '/mood': Mood,
  '/analytics': Analytics,
  '/settings': Settings,
};

export default function App() {
  const path = useHashRoute();
  const route = findRoute(path);
  const overview = useOverview();
  const theme = useTheme();

  const [collapsed, setCollapsed] = useState<boolean>(() =>
    readFlag(STORAGE_KEYS.sidebarCollapsed, false),
  );
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [quickOpen, setQuickOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    writeFlag(STORAGE_KEYS.sidebarCollapsed, collapsed);
  }, [collapsed]);

  useEffect(() => {
    const current = findRoute(path);
    document.title = current ? `${current.title} — Life OS` : 'Страница не найдена — Life OS';
    window.scrollTo({ top: 0, behavior: 'auto' });
  }, [path]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        setQuickOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const modalOpen = paletteOpen || quickOpen;
  useEffect(() => {
    if (!modalOpen) return undefined;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [modalOpen]);

  const openPalette = useCallback(() => {
    setQuickOpen(false);
    setPaletteOpen(true);
  }, []);

  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const openQuickEntry = useCallback(() => {
    setPaletteOpen(false);
    setQuickOpen(true);
  }, []);
  const closeQuickEntry = useCallback(() => setQuickOpen(false), []);
  const toggleCollapsed = useCallback(() => setCollapsed((value) => !value), []);

  const handleRefresh = useCallback(() => {
    void overview.refresh();
  }, [overview.refresh]);

  const handleAddWater = useCallback(
    async (ml: number): Promise<void> => {
      const dataSource = await overview.addWater(ml);
      toast(
        dataSource === 'api'
          ? `Вода: +${ml} мл записано`
          : `Вода: +${ml} мл сохранено локально`,
        'success',
      );
    },
    [overview.addWater],
  );

  const handleAddWaterCommand = useCallback(() => {
    void handleAddWater(250);
  }, [handleAddWater]);

  const handleQuickSubmit = useCallback(
    async (input: QuickEntryInput): Promise<void> => {
      const result = await postQuick(input);
      toast(
        result.source === 'api'
          ? 'Запись сохранена и попадёт в журнал'
          : 'Запись сохранена в демо-режиме',
        'success',
      );
      void overview.refresh();
    },
    [overview.refresh],
  );

  const focusMain = () => mainRef.current?.focus();

  const Page = PAGE_COMPONENTS[path];

  return (
    <div className="app" data-sidebar={collapsed ? 'collapsed' : 'expanded'}>
      <button type="button" className="skip-link" onClick={focusMain}>
        Перейти к содержимому
      </button>

      <Sidebar
        path={path}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
        badges={overview.data?.nav ?? null}
        themeMode={theme.mode}
        onThemeMode={theme.setMode}
        source={overview.source}
        sync={overview.data?.sync ?? null}
        refreshing={overview.refreshing}
        onRefresh={handleRefresh}
      />

      <div className="app__main">
        <Topbar route={route} onOpenSearch={openPalette} onQuickEntry={openQuickEntry} />

        <main className="content" id="main-content" ref={mainRef} tabIndex={-1}>
          {path === '/' ? (
            <Overview
              data={overview.data}
              source={overview.source}
              refreshing={overview.refreshing}
              onAddWater={(ml) => {
                void handleAddWater(ml);
              }}
            />
          ) : Page ? (
            <Page />
          ) : route?.soon ? (
            <ComingSoon route={route} />
          ) : (
            <NotFound />
          )}
        </main>
      </div>

      <MobileNav
        path={path}
        badges={overview.data?.nav ?? null}
        themeMode={theme.mode}
        onThemeMode={theme.setMode}
      />

      <button
        type="button"
        className="fab"
        onClick={openQuickEntry}
        aria-label="Быстрая запись"
        title="Быстрая запись"
      >
        <Icon name="plus" size={20} />
      </button>

      <CommandPalette
        open={paletteOpen}
        onClose={closePalette}
        onNavigate={navigate}
        onQuickEntry={openQuickEntry}
        onAddWater={handleAddWaterCommand}
        onToggleTheme={theme.cycleMode}
        onToggleSidebar={toggleCollapsed}
        onRefresh={handleRefresh}
        badges={overview.data?.nav ?? null}
        themeMode={theme.mode}
      />

      <QuickEntryDialog
        open={quickOpen}
        onClose={closeQuickEntry}
        onSubmit={handleQuickSubmit}
      />

      <Toaster />
    </div>
  );
}
