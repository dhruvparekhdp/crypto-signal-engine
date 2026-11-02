import { ChangeDetectionStrategy, Component, input, signal } from '@angular/core';

export interface DataColumn {
  key: string;
  label: string;
}

@Component({
  selector: 'ui-data-table',
  standalone: true,
  template: `
    @if (!rows().length) {
      <p class="dim">No rows.</p>
    } @else {
      <div class="wrap">
        <table>
          <thead>
            <tr>
              @for (c of columns(); track c.key) {
                <th (click)="sortBy(c.key)">{{ c.label }}</th>
              }
            </tr>
          </thead>
          <tbody>
            @for (r of sorted(); track $index) {
              <tr>
                @for (c of columns(); track c.key) {
                  <td [attr.data-label]="c.label">{{ cell(r, c.key) }}</td>
                }
              </tr>
            }
          </tbody>
        </table>
      </div>
    }
  `,
  styles: [
    `
      .wrap {
        overflow: auto;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
      }
      th {
        position: sticky;
        top: 0;
        background: var(--surface-3);
        text-align: left;
        padding: 0.5rem;
        cursor: pointer;
      }
      td {
        padding: 0.5rem;
        border-top: 1px solid var(--edge);
      }
      @media (max-width: 640px) {
        thead {
          display: none;
        }
        tr {
          display: block;
          margin-bottom: 0.75rem;
          border: 1px solid var(--edge);
          border-radius: var(--radius-md);
          padding: 0.5rem;
        }
        td {
          display: flex;
          justify-content: space-between;
          border: none;
        }
        td::before {
          content: attr(data-label);
          color: var(--dim);
        }
      }
      .dim {
        color: var(--dim);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataTableComponent {
  readonly columns = input<DataColumn[]>([]);
  readonly rows = input<Record<string, unknown>[]>([]);
  private sortKey = signal('');
  private sortDir = signal(1);

  sorted(): Record<string, unknown>[] {
    const rows = [...this.rows()];
    const k = this.sortKey();
    if (!k) return rows;
    const dir = this.sortDir();
    return rows.sort((a, b) => {
      const av = a[k];
      const bv = b[k];
      if (av === bv) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      return av > bv ? dir : -dir;
    });
  }

  sortBy(key: string): void {
    if (this.sortKey() === key) this.sortDir.update((d) => -d);
    else {
      this.sortKey.set(key);
      this.sortDir.set(1);
    }
  }

  cell(r: Record<string, unknown>, key: string): string {
    const v = r[key];
    return v == null ? '—' : String(v);
  }
}
