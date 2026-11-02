import { ChangeDetectionStrategy, Component, HostListener, ViewChild, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { NAV, appPath } from '../core/nav.model';
import { CommandBarComponent } from '../shared/ui/command-bar.component';
import { ThemeService, LookId } from '../shared/ui/theme.service';

@Component({
  standalone: true,
  selector: 'app-shell',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, CommandBarComponent],
  template: `
    <header class="top">
      <a routerLink="/app/command/overview" class="brand">Signal engine</a>
      <button type="button" class="cmd" (click)="commandBar.toggle()">⌘K</button>
      <select class="look" aria-label="Theme look" [value]="theme.look()" (change)="onLook($event)">
        @for (l of looks; track l) {
          <option [value]="l">{{ l }}</option>
        }
      </select>
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
    <ui-command-bar />
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
      .cmd,
      .look {
        min-height: 44px;
        border: 1px solid var(--edge);
        background: var(--surface-1);
        color: var(--ink);
        border-radius: var(--radius-sm);
        padding: 0 0.65rem;
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
        min-height: 44px;
        display: flex;
        align-items: center;
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
  @ViewChild(CommandBarComponent) commandBar!: CommandBarComponent;
  readonly nav = NAV;
  readonly theme = inject(ThemeService);
  readonly looks: LookId[] = ['a', 'b', 'c', 'brutal'];

  link(hub: string, tab: string): string {
    return appPath(hub, tab);
  }

  onLook(ev: Event): void {
    const v = (ev.target as HTMLSelectElement).value as LookId;
    this.theme.setLook(v);
  }

  @HostListener('document:keydown', ['$event'])
  onKey(e: KeyboardEvent): void {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      this.commandBar?.toggle();
    }
  }
}
