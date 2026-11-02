import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'ui-empty-state',
  standalone: true,
  template: `<p class="empty">{{ message() }}</p>`,
  styles: [` .empty { color: var(--dim); margin: 0; } `],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EmptyStateComponent {
  readonly message = input('Nothing to show.');
}
