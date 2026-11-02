import { Routes } from '@angular/router';
import { NAV, appPath } from './core/nav.model';

function hubRoutes(hubId: string): Routes {
  const hub = NAV.find((h) => h.id === hubId)!;
  return hub.tabs.map((t) => ({
    path: t.id,
    loadComponent: () =>
      import('./features/placeholder/placeholder.page').then((m) => m.PlaceholderPage),
    data: { title: t.label, path: appPath(hubId, t.id) },
  }));
}

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'app/command/overview' },
  {
    path: 'app/sign-in',
    loadComponent: () => import('./features/sign-in/sign-in.page').then((m) => m.SignInPage),
  },
  {
    path: 'app/_kit',
    loadComponent: () => import('./features/kit/kit.page').then((m) => m.KitPage),
  },
  {
    path: 'app',
    loadComponent: () => import('./layout/app-shell.component').then((m) => m.AppShellComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'command/overview' },
      { path: 'command', children: hubRoutes('command') },
      { path: 'book', children: hubRoutes('book') },
      { path: 'evidence', children: hubRoutes('evidence') },
      { path: 'system', children: hubRoutes('system') },
    ],
  },
];
