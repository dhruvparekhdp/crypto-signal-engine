import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ApiClient } from '../core/api-client.service';
import { PanelComponent } from '../shared/ui/panel.component';
@Component({
  standalone: true,
  selector: 'app-moves-page',
  imports: [PanelComponent],
  template: `
    <ui-panel title="Market moves" [loading]="loading()" [error]="error()" (retry)="load()">
      @if (latest()) {
        <p class="lead">{{ latest()?.result?.overall }}</p>
        <div class="grid">
          @for (c of latest()?.result?.coins || []; track c.symbol) {
            <article class="coin">
              <header>{{ c.symbol }} · {{ c.cause_type }}</header>
              <p>{{ c.cause }}</p>
            </article>
          }
        </div>
        <h3>Earlier</h3>
        <ul>
          @for (r of runs(); track r.id) {
            <li>{{ r.overall_preview }} · {{ r.news_driven }} news</li>
          }
        </ul>
      }
    </ui-panel>
  `,
  styles: [
    `
      .lead {
        font-size: 1.05rem;
      }
      .grid {
        display: grid;
        gap: 0.75rem;
      }
      @media (min-width: 640px) {
        .grid {
          grid-template-columns: 1fr 1fr;
        }
      }
      .coin {
        border: 1px solid var(--edge);
        border-radius: var(--radius-md);
        padding: 0.75rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MovesPage implements OnInit {
  private readonly api = inject(ApiClient);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly latest = signal<any>(null);
  readonly runs = signal<any[]>([]);

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    try {
      const r = await this.api.get<any>('/api/moves?limit=24');
      this.latest.set(r.data?.latest);
      this.runs.set(r.data?.runs ?? []);
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Failed');
    } finally {
      this.loading.set(false);
    }
  }
}
