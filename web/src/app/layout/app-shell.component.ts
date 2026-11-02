import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { NAV, appPath } from '../core/nav.model';

@Component({
  standalone: true,
  selector: 'app-shell',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <header class="top">
      <a routerLink="/app/command/overview" class="brand">Signal engine</a>
      <nav class="hubs">
        @for (h of nav; track h.id) {
          <details>
            <summary>{{ h.label }}</summary>
            <div class="tabs">
              @for (t of h.tabs; track t.id) {
                <a [routerLink]="link(h.id, t.id)" routerLinkActive="on">{{ t.label }}</a>
              }
            </div>
          </details>
        }
      </nav>
    </header>
    <main class="main"><router-outlet /></main>
  `,
  styles: [
    `
      .top {
        display: flex;
        flex-wrap: wrap;
        gap: 0.75rem;
        align-items: center;
        padding: 0.75rem 1rem;
        border-bottom: 1px solid var(--edge);
      }
      .brand {
        font-family: var(--font-display);
        letter-spacing: 0.12em;
        text-transform: uppercase;
        text-decoration: none;
        color: var(--ink);
      }
      .hubs {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
      }
      summary {
        cursor: pointer;
        list-style: none;
        font-size: 0.85rem;
      }
      .tabs {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
        padding: 0.5rem;
      }
      .tabs a {
        color: var(--dim);
        text-decoration: none;
        font-size: 0.85rem;
      }
      .tabs a.on {
        color: var(--accent-1);
      }
      .main {
        min-height: 60vh;
        padding: 1rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AppShellComponent {
  readonly nav = NAV;

  link(hub: string, tab: string): string {
    return appPath(hub, tab);
  }
}
