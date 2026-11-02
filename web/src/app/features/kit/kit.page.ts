import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { PanelComponent } from '../../shared/ui/panel.component';
import { StatComponent } from '../../shared/ui/stat.component';
import { PillComponent } from '../../shared/ui/pill.component';
import { MoneyComponent } from '../../shared/ui/money.component';
import { RbarComponent } from '../../shared/ui/rbar.component';
import { LevelChipComponent } from '../../shared/ui/level-chip.component';
import { SkeletonComponent } from '../../shared/ui/skeleton.component';
import { EmptyStateComponent } from '../../shared/ui/empty-state.component';
import { ErrorStateComponent } from '../../shared/ui/error-state.component';
import { DataTableComponent, DataColumn } from '../../shared/ui/data-table.component';
import { TimeAgoComponent } from '../../shared/ui/time-ago.component';
import { ThemeService, LookId } from '../../shared/ui/theme.service';

@Component({
  standalone: true,
  selector: 'app-kit',
  imports: [
    PanelComponent,
    StatComponent,
    PillComponent,
    MoneyComponent,
    RbarComponent,
    LevelChipComponent,
    SkeletonComponent,
    EmptyStateComponent,
    ErrorStateComponent,
    DataTableComponent,
    TimeAgoComponent,
  ],
  template: `
    <ui-panel title="UI kit">
      <div class="looks">
        @for (l of looks; track l) {
          <button type="button" [class.on]="theme.look() === l" (click)="theme.setLook(l)">{{ l }}</button>
        }
      </div>
      <div class="row">
        <ui-stat label="Sample stat" value="42" />
        <ui-pill text="LONG" kind="long" />
        <ui-pill text="SHORT" kind="short" />
        <ui-money [amount]="125000" />
        <ui-money [amount]="-4200" [signed]="true" />
        <ui-level-chip [level]="3" />
        <ui-time-ago [iso]="iso()" />
      </div>
      <ui-rbar [stop]="90" [entry]="100" [now]="105" [target]="115" [min]="88" [max]="118" />
      <ui-skeleton />
      <ui-empty-state message="Nothing here — try another tab" />
      <ui-error-state message="Example error" (retry)="retryCount.set(retryCount() + 1)" />
      <ui-data-table [columns]="cols" [rows]="rows" />
      <p class="dim">Retries: {{ retryCount() }}</p>
    </ui-panel>
  `,
  styles: [
    `
      .looks {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-bottom: 1rem;
      }
      .looks button {
        min-height: 44px;
        padding: 0 0.75rem;
        border: 1px solid var(--edge);
        background: var(--surface-1);
        color: var(--ink);
        border-radius: var(--radius-sm);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.75rem;
      }
      .looks button.on {
        border-color: var(--accent-1);
        color: var(--accent-1);
      }
      .row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.75rem;
        align-items: center;
        margin-bottom: 1rem;
      }
      .dim {
        color: var(--dim);
        font-size: 0.85rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class KitPage {
  readonly theme = inject(ThemeService);
  readonly looks: LookId[] = ['a', 'b', 'c', 'brutal'];
  readonly iso = signal(new Date(Date.now() - 120_000).toISOString());
  readonly retryCount = signal(0);
  readonly cols: DataColumn[] = [
    { key: 'sym', label: 'Symbol' },
    { key: 'side', label: 'Side' },
  ];
  readonly rows = [
    { sym: 'BTC', side: 'long' },
    { sym: 'ETH', side: 'short' },
  ];
}
