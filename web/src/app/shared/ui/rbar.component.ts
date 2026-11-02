import { ChangeDetectionStrategy, Component, input } from '@angular/core';

/** Displays stop · entry · now · target from API fields only. */
@Component({
  selector: 'ui-rbar',
  standalone: true,
  template: `
    <div class="rbar" role="img" [attr.aria-label]="aria()">
      <i class="stop" [style.left.%]="pct(stop())"></i>
      <i class="entry" [style.left.%]="pct(entry())"></i>
      <i class="now" [style.left.%]="pct(now())"></i>
      <i class="target" [style.left.%]="pct(target())"></i>
    </div>
  `,
  styles: [
    `
      .rbar {
        position: relative;
        height: 8px;
        background: var(--surface-2);
        border-radius: 4px;
        margin: 0.5rem 0;
      }
      .rbar i {
        position: absolute;
        top: -4px;
        width: 3px;
        height: 16px;
        transform: translateX(-50%);
      }
      .stop {
        background: var(--down);
      }
      .entry {
        background: var(--dim);
      }
      .now {
        background: var(--accent-1);
        width: 6px;
      }
      .target {
        background: var(--up);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RbarComponent {
  readonly stop = input<number | null>(null);
  readonly entry = input<number | null>(null);
  readonly now = input<number | null>(null);
  readonly target = input<number | null>(null);
  readonly min = input(0);
  readonly max = input(1);

  pct(v: number | null): number {
    if (v == null) return 0;
    const lo = this.min();
    const hi = this.max();
    if (hi <= lo) return 50;
    return Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
  }

  aria(): string {
    return `Stop ${this.stop()}, entry ${this.entry()}, now ${this.now()}, target ${this.target()}`;
  }
}
