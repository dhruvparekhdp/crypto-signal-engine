import { ChangeDetectionStrategy, Component } from '@angular/core';

@Component({
  standalone: true,
  selector: 'app-kit',
  template: `<p class="dim">UI kit demo — see design-system commit.</p>`,
  styles: [` .dim { color: var(--dim); padding: 1rem; } `],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class KitPage {}
