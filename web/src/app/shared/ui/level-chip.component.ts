import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'ui-level-chip',
  standalone: true,
  template: `<span class="lv" [class]="'l' + level()">L{{ level() }}</span>`,
  styles: [
    `
      .lv {
        font-family: var(--font-display);
        font-size: 0.75rem;
        padding: 0.2rem 0.45rem;
        border-radius: var(--radius-sm);
        border: 1px solid var(--edge);
      }
      .l5,
      .l4 {
        color: var(--down);
      }
      .l3 {
        color: var(--warn);
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LevelChipComponent {
  readonly level = input(1);
}
