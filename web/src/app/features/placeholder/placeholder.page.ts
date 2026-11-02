import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';

@Component({
  standalone: true,
  selector: 'app-placeholder-page',
  template: `
    <section class="panel">
      <h1>{{ title }}</h1>
      <p class="dim">This page is being rebuilt in the new app. The classic portal still has the live view.</p>
      <p class="mono">{{ path }}</p>
    </section>
  `,
  styles: [
    `
      .panel {
        padding: 1.25rem;
      }
      .dim {
        color: var(--dim);
      }
      .mono {
        font-family: var(--font-data);
        font-size: 0.85rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PlaceholderPage {
  private readonly route = inject(ActivatedRoute);
  title = String(this.route.snapshot.data['title'] ?? 'Page');
  path = String(this.route.snapshot.data['path'] ?? '');
}
