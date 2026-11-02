import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ApiClient } from '../core/api-client.service';
import { PanelComponent } from '../shared/ui/panel.component';
import { StatComponent } from '../shared/ui/stat.component';
import { RbarComponent } from '../shared/ui/rbar.component';
import { MoneyComponent } from '../shared/ui/money.component';
import { DataTableComponent, DataColumn } from '../shared/ui/data-table.component';
import { PillComponent } from '../shared/ui/pill.component';

@Component({
  standalone: true,
  selector: 'app-live-page',
  imports: [PanelComponent, StatComponent, RbarComponent, MoneyComponent, DataTableComponent, PillComponent],
  template: `
    <ui-panel title="Live ₹ book" [loading]="loading()" [error]="error()" (retry)="load()">
      @if (data()) {
        <div class="grid">
          <ui-stat label="Equity" [value]="inr(data()?.wallet?.equity_inr)" />
          <ui-stat label="Open P&amp;L" [value]="inr(data()?.wallet?.upnl_inr)" />
          <ui-stat label="Free margin" [value]="inr(data()?.wallet?.free_inr)" />
        </div>
        @for (p of data()?.positions || []; track p.symbol) {
          <article class="pos">
            <header>
              <ui-pill [text]="p.side" [kind]="p.side" />
              <strong>{{ p.symbol }}</strong>
              <span class="tabular">R {{ p.r_now ?? '—' }}</span>
            </header>
            <ui-rbar [stop]="p.init_stop" [entry]="p.entry" [now]="p.mark" [target]="p.exchange_target" [min]="barMin(p)" [max]="barMax(p)" />
            <ui-money [amount]="p.upnl_inr ?? 0" [signed]="true" />
          </article>
        }
        <ui-data-table [columns]="orderCols" [rows]="data()?.orders || []" />
      }
    </ui-panel>
  `,
  styles: [
    `
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
        gap: 0.75rem;
      }
      .pos {
        border-top: 1px solid var(--edge);
        padding: 0.75rem 0;
      }
      header {
        display: flex;
        gap: 0.5rem;
        align-items: center;
        flex-wrap: wrap;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LivePage implements OnInit {
  private readonly api = inject(ApiClient);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly data = signal<any>(null);
  readonly orderCols: DataColumn[] = [
    { key: 'symbol', label: 'Symbol' },
    { key: 'side', label: 'Side' },
    { key: 'size', label: 'Size' },
  ];

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const r = await this.api.get<any>('/api/delta/live');
      this.data.set(r.data);
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Failed');
    } finally {
      this.loading.set(false);
    }
  }

  inr(v: unknown): string {
    return v == null ? '—' : String(v);
  }

  barMin(p: { init_stop?: number; entry?: number; mark?: number; exchange_target?: number }): number {
    return Math.min(p.init_stop ?? p.entry ?? 0, p.entry ?? 0, p.mark ?? 0, p.exchange_target ?? 0);
  }

  barMax(p: { init_stop?: number; entry?: number; mark?: number; exchange_target?: number }): number {
    return Math.max(p.init_stop ?? 0, p.entry ?? 0, p.mark ?? 0, p.exchange_target ?? 0);
  }
}
