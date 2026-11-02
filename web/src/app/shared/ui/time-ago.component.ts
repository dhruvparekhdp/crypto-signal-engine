import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: 'ui-time-ago',
  standalone: true,
  template: `<time [dateTime]="iso()">{{ label() }}</time>`,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TimeAgoComponent {
  readonly iso = input('');
  label(): string {
    const t = Date.parse(this.iso());
    if (!t) return '—';
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return `${m}m ago`;
    if (m < 2880) return `${Math.round(m / 60)}h ago`;
    return `${Math.round(m / 1440)}d ago`;
  }
}
