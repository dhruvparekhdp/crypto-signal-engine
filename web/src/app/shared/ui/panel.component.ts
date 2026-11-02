import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { EmptyStateComponent } from './empty-state.component';
import { ErrorStateComponent } from './error-state.component';
import { SkeletonComponent } from './skeleton.component';

@Component({
  selector: 'ui-panel',
  standalone: true,
  imports: [EmptyStateComponent, ErrorStateComponent, SkeletonComponent],
  template: `
    <section class="panel">
      @if (title()) {
        <header><h2>{{ title() }}</h2></header>
      }
      @if (loading()) {
        <ui-skeleton />
      } @else if (error()) {
        <ui-error-state [message]="error()!" (retry)="retry.emit()" />
      } @else if (empty()) {
        <ui-empty-state [message]="emptyMessage()" />
      } @else {
        <ng-content />
      }
    </section>
  `,
  styles: [
    `
      .panel {
        background: var(--glass);
        border: 1px solid var(--edge);
        border-radius: var(--radius-lg);
        padding: var(--space-4);
        margin-bottom: var(--space-3);
      }
      h2 {
        margin: 0 0 var(--space-3);
        font-family: var(--font-display);
        font-size: 0.95rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PanelComponent {
  readonly title = input<string | null>(null);
  readonly loading = input(false);
  readonly error = input<string | null>(null);
  readonly empty = input(false);
  readonly emptyMessage = input('Nothing here yet.');
  readonly retry = output<void>();
}
