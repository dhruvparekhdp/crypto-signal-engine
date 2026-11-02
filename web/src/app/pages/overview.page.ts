import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ApiClient } from '../core/api-client.service';
import { PanelComponent } from '../shared/ui/panel.component';
import { StatComponent } from '../shared/ui/stat.component';

@Component({
  standalone: true,
  selector: 'app-overview-page',
  imports: [PanelComponent, StatComponent],
  template: `
    <ui-panel title="Overview" [loading]="loading()" [error]="error()" (retry)="load()">
      <div class="grid">
        <ui-stat label="Watchlist coins" [value]="coinsLabel()" />
        <ui-stat label="Engine" [value]="statusLabel()" />
        <ui-stat label="Paper mode" [value]="metaLabel()" />
      </div>
    </ui-panel>
  `,
  styles: [
    `
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
        gap: 0.75rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OverviewPage implements OnInit {
  private readonly api = inject(ApiClient);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly coins = signal(0);
  readonly statusLabel = signal('—');
  readonly metaLabel = signal('—');

  coinsLabel(): string {
    return String(this.coins());
  }

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    try {
      const [coins, status, meta] = await Promise.all([
        this.api.get<{ coins?: unknown[] }>('/api/crypto/coins'),
        this.api.get<{ ok?: boolean; engine?: string }>('/api/status'),
        this.api.get<{ paper?: boolean }>('/api/portal/meta'),
      ]);
      const raw = coins.data?.coins ?? coins.data;
      this.coins.set(Array.isArray(raw) ? raw.length : 0);
      this.statusLabel.set(String(status.data?.engine ?? status.data?.ok ?? '—'));
      this.metaLabel.set(meta.data?.paper ? 'Paper' : 'Live view');
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Failed');
    } finally {
      this.loading.set(false);
    }
  }
}
