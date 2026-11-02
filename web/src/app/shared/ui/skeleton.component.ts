import { ChangeDetectionStrategy, Component } from '@angular/core';

@Component({
  selector: 'ui-skeleton',
  standalone: true,
  template: `<div class="sk" aria-hidden="true"></div>`,
  styles: [
    `
      .sk {
        height: 4rem;
        border-radius: var(--radius-md);
        background: linear-gradient(90deg, var(--surface-2), var(--surface-1), var(--surface-2));
        animation: pulse 1.2s ease infinite;
      }
      @media (prefers-reduced-motion: reduce) {
        .sk {
          animation: none;
        }
      }
      @keyframes pulse {
        0% {
          opacity: 0.6;
        }
        50% {
          opacity: 1;
        }
        100% {
          opacity: 0.6;
        }
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SkeletonComponent {}
