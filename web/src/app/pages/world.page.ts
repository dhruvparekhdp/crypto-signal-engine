import { ChangeDetectionStrategy, Component, ElementRef, OnInit, ViewChild, inject, signal } from '@angular/core';
import { ApiClient } from '../core/api-client.service';
import { PanelComponent } from '../shared/ui/panel.component';
import { LevelChipComponent } from '../shared/ui/level-chip.component';
@Component({
  standalone: true,
  selector: 'app-world-page',
  imports: [PanelComponent, LevelChipComponent],
  template: `
    <ui-panel title="World" [loading]="loading()" [error]="error()" (retry)="load()">
      <div #globe class="globe" aria-hidden="true"></div>
      <ul class="events">
        @for (e of events(); track e.id) {
          <li>
            <ui-level-chip [level]="e.level" />
            <strong>{{ e.title }}</strong>
            <span class="dim">{{ e.source }}</span>
          </li>
        }
      </ul>
    </ui-panel>
  `,
  styles: [
    `
      .globe {
        height: min(42vh, 320px);
        border-radius: var(--radius-lg);
        border: 1px solid var(--edge);
        margin-bottom: 1rem;
      }
      .events {
        list-style: none;
        padding: 0;
        margin: 0;
      }
      li {
        display: flex;
        gap: 0.5rem;
        align-items: baseline;
        padding: 0.5rem 0;
        border-top: 1px solid var(--edge);
        flex-wrap: wrap;
      }
      .dim {
        color: var(--dim);
        font-size: 0.8rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class WorldPage implements OnInit {
  @ViewChild('globe', { static: true }) globeEl!: ElementRef<HTMLDivElement>;
  private readonly api = inject(ApiClient);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly events = signal<any[]>([]);

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    try {
      const r = await this.api.get<any>('/api/portal/world');
      if (r.data?.enabled === false) {
        this.error.set(r.data.message || 'News engine off');
        return;
      }
      this.events.set(r.data?.events ?? []);
      await this.mountGlobe(r.data?.heat ?? {});
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : 'Failed');
    } finally {
      this.loading.set(false);
    }
  }

  private async mountGlobe(heat: Record<string, number>): Promise<void> {
    try {
      const mod = await import('globe.gl');
      const Globe = mod.default as unknown as (el: HTMLElement) => {
        globeImageUrl: (u: string) => { backgroundColor: (c: string) => { pointsData: (d: unknown[]) => void } };
      };
      const el = this.globeEl.nativeElement;
      const g = Globe(el)
        .globeImageUrl('//unpkg.com/three-globe/example/img/earth-dark.jpg')
        .backgroundColor('rgba(0,0,0,0)');
      const pts = Object.entries(heat).map(([k, v]) => ({ lat: 20, lng: 0, size: v, label: k }));
      g.pointsData(pts);
    } catch {
      /* globe optional offline */
    }
  }
}
