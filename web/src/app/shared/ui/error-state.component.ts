import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

@Component({
  selector: 'ui-error-state',
  standalone: true,
  template: `
    <div class="err">
      <p>{{ message() }}</p>
      <button type="button" (click)="retry.emit()">Try again</button>
    </div>
  `,
  styles: [
    `
      .err {
        color: var(--warn);
      }
      button {
        margin-top: 0.5rem;
        min-height: 44px;
        min-width: 44px;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ErrorStateComponent {
  readonly message = input('Something went wrong.');
  readonly retry = output<void>();
}
