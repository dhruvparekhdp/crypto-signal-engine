import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'ui-pill',
  standalone: true,
  template: `<span class="pill" [class]="kind()">{{ text() }}</span>`,
  styles: [
    `
      .pill {
        display: inline-block;
        padding: 0.15rem 0.5rem;
        border-radius: 999px;
        border: 1px solid var(--edge);
        font-size: 0.75rem;
        font-family: var(--font-data);
      }
      .long {
        color: var(--up);
      }
      .short {
        color: var(--down);
      }
      .won {
        border-color: var(--up);
      }
      .lost {
        border-color: var(--down);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PillComponent {
  readonly text = input('');
  readonly kind = input('neutral');
}
