export type PageMeta =
  | { kind: 'live' }
  | { kind: 'moves' }
  | { kind: 'signals' }
  | { kind: 'overview' }
  | { kind: 'world' }
  | { kind: 'generic'; title: string; endpoints: string[] };

export const PAGE_META: Record<string, PageMeta> = {
  'book/live': { kind: 'live' },
  'book/swing': { kind: 'generic', title: 'Swing book', endpoints: ['/api/swing', '/api/paper'] },
  'book/paper': { kind: 'generic', title: 'Paper cycle', endpoints: ['/api/paper', '/api/candidates', '/api/bracket', '/api/vol'] },
  'book/guard': { kind: 'generic', title: 'Session guard', endpoints: ['/api/parity', '/api/status'] },
  'book/simulator': { kind: 'generic', title: 'Simulator', endpoints: ['/api/simulator/status'] },
  'book/intraday-sim': { kind: 'generic', title: 'Intraday sim', endpoints: ['/api/simulator/status'] },
  'book/journal': { kind: 'generic', title: 'Journal', endpoints: ['/api/delta/live'] },
  'command/overview': { kind: 'overview' },
  'command/signals': { kind: 'signals' },
  'command/watchlist': { kind: 'generic', title: 'Watchlist', endpoints: ['/api/crypto/coins'] },
  'command/outlook': { kind: 'generic', title: 'Price outlook', endpoints: ['/api/predict'] },
  'command/moves': { kind: 'moves' },
  'command/chart': { kind: 'generic', title: 'Chart', endpoints: ['/api/crypto/coins'] },
  'command/world': { kind: 'world' },
  'command/events': { kind: 'generic', title: 'Events', endpoints: ['/api/events', '/api/portal/news'] },
  'command/calendar': { kind: 'generic', title: 'Calendar', endpoints: ['/api/portal/news'] },
  'evidence/strategies': { kind: 'generic', title: 'Strategies', endpoints: ['/api/research'] },
  'evidence/accuracy': { kind: 'generic', title: 'Accuracy', endpoints: ['/api/signals/accuracy'] },
  'evidence/history': { kind: 'generic', title: 'History', endpoints: ['/api/signals/history?days=7&limit=200&cursor='] },
  'evidence/research': { kind: 'generic', title: 'Research', endpoints: ['/api/research'] },
  'evidence/news-study': { kind: 'generic', title: 'News study', endpoints: ['/api/portal/news'] },
  'evidence/audit': { kind: 'generic', title: 'Audit', endpoints: ['/api/status'] },
  'evidence/mirror': { kind: 'generic', title: 'Mirror', endpoints: ['/api/status'] },
  'evidence/pipeline': { kind: 'generic', title: 'Pipeline', endpoints: ['/api/debug'] },
  'evidence/v2': { kind: 'generic', title: 'v2 shadow', endpoints: ['/api/v2/shadow'] },
  'system/golive': { kind: 'generic', title: 'Go-live', endpoints: ['/api/golive'] },
  'system/health': { kind: 'generic', title: 'Health', endpoints: ['/api/status', '/api/debug'] },
  'system/ai': { kind: 'generic', title: 'AI', endpoints: ['/api/llm/budget'] },
  'system/data': { kind: 'generic', title: 'Data', endpoints: ['/api/tables?limit=50'] },
  'system/diagnostics': { kind: 'generic', title: 'Diagnostics', endpoints: ['/api/debug/perf'] },
  'system/settings': { kind: 'generic', title: 'Settings', endpoints: ['/api/settings'] },
  'system/keys': { kind: 'generic', title: 'Keys', endpoints: ['/api/settings/auth/status'] },
  'system/api': { kind: 'generic', title: 'API list', endpoints: ['/api/status'] },
};
