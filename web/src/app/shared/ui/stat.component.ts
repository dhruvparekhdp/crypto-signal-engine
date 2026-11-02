import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'ui-stat',
  standalone: true,
  template: `
    <div class="stat">
      <small>{{ label() }}</small>
      <strong class="tabular">{{ value() }}</strong>
      @if (delta()) {
        <span class="delta" [class.up]="deltaPositive()" [class.dn]="!deltaPositive()">{{
          deltaPositive() ? '▲' : '▼'
        }}
          {{ delta() }}</span>
      }
    </div>
  `,
  styles: [
    `
      .stat small {
        display: block;
        color: var(--dim);
        font-size: 0.75rem;
      }
      .stat strong {
        font-size: 1.4rem;
        font-family: var(--font-display);
      }
      .delta.up {
        color: var(--up);
      }
      .delta.dn {
        color: var(--down);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StatComponent {
  readonly label = input('');
  readonly value = input('—');
  readonly delta = input<string | null>(null);
  readonly deltaPositive = input(true);
}
