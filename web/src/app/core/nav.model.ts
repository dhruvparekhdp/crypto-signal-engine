/** Mirrors scheduler/portal/nav.js — single source for /app routes. */
export interface NavTab {
  id: string;
  label: string;
  native?: boolean;
  url?: string;
}

export interface NavHub {
  id: string;
  label: string;
  tabs: NavTab[];
}

export const NAV: NavHub[] = [
  {
    id: 'command',
    label: 'Command',
    tabs: [
      { id: 'overview', label: 'Overview', native: true },
      { id: 'signals', label: 'Signals', native: true },
      { id: 'watchlist', label: 'Watchlist', native: true },
      { id: 'outlook', label: 'Price outlook' },
      { id: 'moves', label: 'Market moves' },
      { id: 'chart', label: 'Chart' },
      { id: 'world', label: 'World' },
      { id: 'events', label: 'Events', native: true },
      { id: 'calendar', label: 'Calendar', native: true },
    ],
  },
  {
    id: 'book',
    label: 'Book',
    tabs: [
      { id: 'swing', label: 'Swing book', native: true },
      { id: 'paper', label: 'Paper cycle', native: true },
      { id: 'live', label: 'Live ₹ (real)', native: true },
      { id: 'guard', label: 'Session guard', native: true },
      { id: 'simulator', label: 'Simulator', native: true },
      { id: 'intraday-sim', label: 'Intraday sim (old)' },
      { id: 'journal', label: 'Journal' },
    ],
  },
  {
    id: 'evidence',
    label: 'Evidence',
    tabs: [
      { id: 'strategies', label: 'Strategies', native: true },
      { id: 'accuracy', label: 'Accuracy', native: true },
      { id: 'history', label: 'History', native: true },
      { id: 'research', label: 'Research', native: true },
      { id: 'news-study', label: 'News study', native: true },
      { id: 'audit', label: 'Audit' },
      { id: 'mirror', label: 'Mirror' },
      { id: 'pipeline', label: 'Pipeline' },
      { id: 'v2', label: 'v2 shadow' },
    ],
  },
  {
    id: 'system',
    label: 'System',
    tabs: [
      { id: 'golive', label: 'Go-live', native: true },
      { id: 'health', label: 'Health', native: true },
      { id: 'ai', label: 'AI', native: true },
      { id: 'data', label: 'Data', native: true },
      { id: 'diagnostics', label: 'Diagnostics', native: true },
      { id: 'settings', label: 'Settings' },
      { id: 'keys', label: 'Keys' },
      { id: 'api', label: 'API list' },
    ],
  },
];

export function appPath(hubId: string, tabId: string): string {
  return `/app/${hubId}/${tabId}`;
}
