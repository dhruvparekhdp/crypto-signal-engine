import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ApiClient } from '../core/api-client.service';
import { PanelComponent } from '../shared/ui/panel.component';
import { DataTableComponent, DataColumn } from '../shared/ui/data-table.component';
@Component({
  standalone: true,
  selector: 'app-signals-page',
  imports: [PanelComponent, DataTableComponent],
  template: `
    <ui-panel title="Signals" [loading]="loading()" [error]="error()" (retry)="load()">
      <ui-data-table [columns]="cols" [rows]="rows()" />
    </ui-panel>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalsPage implements OnInit {
  private readonly api = inject(ApiClient);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly rows = signal<Record<string, unknown>[]>([]);
  readonly cols: DataColumn[] = [
    { key: 'symbol', label: 'Symbol' },
    { key: 'status', label: 'Status' },
    { key: 'signal_type', label: 'Setup' },
    { key: 'direction', label: 'Side' },
  ];

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    try {
      const r = await this.api.get<{ signals?: Record<string, unknown>[] }>('/api/portal/signals?days=14&limit=40');
      this.rows.set(r.data?.signals ?? []);
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Failed');
    } finally {
      this.loading.set(false);
    }
  }
}
