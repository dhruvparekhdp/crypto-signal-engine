import { ChangeDetectionStrategy, Component, OnInit, inject, input, signal } from '@angular/core';
import { ApiClient } from '../core/api-client.service';
import { PanelComponent } from '../shared/ui/panel.component';
import { DataTableComponent, DataColumn } from '../shared/ui/data-table.component';

@Component({
  standalone: true,
  selector: 'app-generic-api-page',
  imports: [PanelComponent, DataTableComponent],
  template: `
    <ui-panel [title]="title()" [loading]="loading()" [error]="error()" (retry)="load()">
      @if (tables().length) {
        @for (t of tables(); track t.name) {
          <h3>{{ t.name }}</h3>
          <ui-data-table [columns]="t.columns" [rows]="t.rows" />
        }
      } @else if (!loading() && !error()) {
        <pre class="raw">{{ preview() }}</pre>
      }
    </ui-panel>
  `,
  styles: [
    `
      .raw {
        font-family: var(--font-data);
        font-size: 0.75rem;
        overflow: auto;
        max-height: 60vh;
        white-space: pre-wrap;
      }
      h3 {
        font-size: 0.85rem;
        color: var(--dim);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GenericApiPage implements OnInit {
  readonly title = input('Page');
  readonly endpoints = input<string[]>([]);
  private readonly api = inject(ApiClient);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly preview = signal('');
  readonly tables = signal<{ name: string; columns: DataColumn[]; rows: Record<string, unknown>[] }[]>([]);

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const outs: unknown[] = [];
      for (const url of this.endpoints()) {
        outs.push((await this.api.get(url)).data);
      }
      this.preview.set(JSON.stringify(outs.length === 1 ? outs[0] : outs, null, 2).slice(0, 8000));
      this.tables.set(this.extractTables(outs));
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Failed');
    } finally {
      this.loading.set(false);
    }
  }

  private extractTables(outs: unknown[]): { name: string; columns: DataColumn[]; rows: Record<string, unknown>[] }[] {
    const tables: { name: string; columns: DataColumn[]; rows: Record<string, unknown>[] }[] = [];
    outs.forEach((o, i) => {
      if (Array.isArray(o) && o.length && typeof o[0] === 'object') {
        const keys = Object.keys(o[0] as object).slice(0, 8);
        tables.push({
          name: `Dataset ${i + 1}`,
          columns: keys.map((k) => ({ key: k, label: k })),
          rows: o as Record<string, unknown>[],
        });
      }
    });
    return tables;
  }
}
