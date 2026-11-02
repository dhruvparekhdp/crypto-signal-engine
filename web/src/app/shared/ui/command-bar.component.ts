import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { NAV, appPath } from '../../core/nav.model';

@Component({
  selector: 'ui-command-bar',
  standalone: true,
  template: `
    @if (open()) {
      <div class="backdrop" (click)="open.set(false)"></div>
      <dialog open class="dlg">
        <input #q type="search" placeholder="Jump to page…" (input)="filter(q.value)" (keydown.escape)="open.set(false)" />
        <ul>
          @for (item of filtered(); track item.path) {
            <li><button type="button" (click)="go(item.path)">{{ item.label }}</button></li>
          }
        </ul>
      </dialog>
    }
  `,
  styles: [
    `
      .backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.5);
        z-index: 40;
      }
      .dlg {
        position: fixed;
        top: 15%;
        left: 50%;
        transform: translateX(-50%);
        width: min(28rem, 92vw);
        border: 1px solid var(--edge);
        background: var(--surface-3);
        color: var(--ink);
        padding: 1rem;
        z-index: 50;
      }
      input {
        width: 100%;
        min-height: 44px;
        margin-bottom: 0.5rem;
      }
      ul {
        list-style: none;
        margin: 0;
        padding: 0;
        max-height: 50vh;
        overflow: auto;
      }
      button {
        width: 100%;
        text-align: left;
        min-height: 44px;
        background: transparent;
        border: none;
        color: var(--ink);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CommandBarComponent {
  private readonly router = inject(Router);
  readonly open = signal(false);
  private readonly items = NAV.flatMap((h) => h.tabs.map((t) => ({ label: `${h.label} · ${t.label}`, path: appPath(h.id, t.id) })));
  readonly filtered = signal(this.items);

  toggle(): void {
    this.open.update((v) => !v);
    if (this.open()) this.filtered.set(this.items);
  }

  filter(q: string): void {
    const s = q.toLowerCase();
    this.filtered.set(this.items.filter((i) => i.label.toLowerCase().includes(s)));
  }

  go(path: string): void {
    this.open.set(false);
    void this.router.navigateByUrl(path);
  }
}
