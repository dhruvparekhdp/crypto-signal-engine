import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'ui-money',
  standalone: true,
  template: `<span class="tabular" [class.up]="signed() && amount() > 0" [class.dn]="signed() && amount() < 0">{{ formatted() }}</span>`,
  styles: [
    `
      .up {
        color: var(--up);
      }
      .dn {
        color: var(--down);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MoneyComponent {
  readonly amount = input(0);
  readonly signed = input(false);

  formatted(): string {
    const a = this.amount();
    const abs = Math.abs(a);
    const grouped =
      abs >= 100000
        ? (abs / 100000).toFixed(2) + ' L'
        : abs.toLocaleString('en-IN', { maximumFractionDigits: abs < 100 ? 2 : 0 });
    const sign = a < 0 ? '−' : this.signed() && a > 0 ? '+' : '';
    return sign + '₹' + grouped;
  }
}
