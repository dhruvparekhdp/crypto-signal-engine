import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { LivePage } from './live.page';
import { GenericApiPage } from './generic-api.page';
import { MovesPage } from './moves.page';
import { SignalsPage } from './signals.page';
import { OverviewPage } from './overview.page';
import { WorldPage } from './world.page';
import { PAGE_META, PageMeta } from './page-meta';

@Component({
  standalone: true,
  selector: 'app-route-host',
  imports: [LivePage, GenericApiPage, MovesPage, SignalsPage, OverviewPage, WorldPage],
  template: `
    @switch (meta().kind) {
      @case ('live') {
        <app-live-page />
      }
      @case ('moves') {
        <app-moves-page />
      }
      @case ('signals') {
        <app-signals-page />
      }
      @case ('overview') {
        <app-overview-page />
      }
      @case ('world') {
        <app-world-page />
      }
      @default {
        <app-generic-api-page [title]="genericTitle()" [endpoints]="genericEndpoints()" />
      }
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RouteHostPage {
  private readonly route = inject(ActivatedRoute);
  readonly meta = computed((): PageMeta => {
    const hub = this.route.parent?.snapshot.url[0]?.path ?? 'command';
    const tab = this.route.snapshot.url[0]?.path ?? 'overview';
    return (
      PAGE_META[`${hub}/${tab}`] ?? {
        kind: 'generic',
        title: `${hub}/${tab}`,
        endpoints: ['/api/status'],
      }
    );
  });
  readonly genericTitle = computed(() => {
    const m = this.meta();
    return m?.kind === 'generic' ? m.title : 'Page';
  });
  readonly genericEndpoints = computed(() => {
    const m = this.meta();
    return m?.kind === 'generic' ? m.endpoints : [];
  });
}
