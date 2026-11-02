import { Injectable, Signal, computed, inject, signal } from '@angular/core';
import { ApiClient } from './api-client.service';

export interface ResourceState<T> {
  data: Signal<T | undefined>;
  loading: Signal<boolean>;
  error: Signal<string | null>;
  updatedAt: Signal<number | null>;
  refresh: () => Promise<void>;
}

interface Entry<T> {
  url: string;
  intervalMs: number;
  data: ReturnType<typeof signal<T | undefined>>;
  loading: ReturnType<typeof signal<boolean>>;
  error: ReturnType<typeof signal<string | null>>;
  updatedAt: ReturnType<typeof signal<number | null>>;
  etag: string | undefined;
  subs: number;
  timer: ReturnType<typeof setInterval> | null;
}

@Injectable({ providedIn: 'root' })
export class ResourceStore {
  private readonly api = inject(ApiClient);
  private readonly entries = new Map<string, Entry<unknown>>();

  resource<T>(key: string, url: string, intervalMs: number): ResourceState<T> {
    let e = this.entries.get(key) as Entry<T> | undefined;
    if (!e) {
      e = {
        url,
        intervalMs,
        data: signal<T | undefined>(undefined),
        loading: signal(false),
        error: signal<string | null>(null),
        updatedAt: signal<number | null>(null),
        etag: undefined,
        subs: 0,
        timer: null,
      };
      this.entries.set(key, e as Entry<unknown>);
      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', () => this.onVisibility());
      }
    }
    e.subs += 1;
    if (e.subs === 1) {
      void this.fetch(key);
      this.startTimer(key);
    }
    return {
      data: e.data.asReadonly(),
      loading: e.loading.asReadonly(),
      error: e.error.asReadonly(),
      updatedAt: e.updatedAt.asReadonly(),
      refresh: () => this.fetch(key),
    };
  }

  release(key: string): void {
    const e = this.entries.get(key);
    if (!e) return;
    e.subs = Math.max(0, e.subs - 1);
    if (e.subs === 0) {
      this.stopTimer(key);
    }
  }

  private async fetch(key: string): Promise<void> {
    const e = this.entries.get(key);
    if (!e || (typeof document !== 'undefined' && document.hidden)) return;
    e.loading.set(true);
    try {
      const r = await this.api.get<unknown>(e.url, { etag: e.etag });
      if (!r.notModified) {
        e.data.set(r.data);
      }
      e.etag = r.etag ?? e.etag;
      e.error.set(null);
      e.updatedAt.set(Date.now());
    } catch (err) {
      e.error.set(err instanceof Error ? err.message : 'Failed to load');
    } finally {
      e.loading.set(false);
    }
  }

  private startTimer(key: string): void {
    const e = this.entries.get(key);
    if (!e || !e.intervalMs || e.timer) return;
    e.timer = setInterval(() => void this.fetch(key), e.intervalMs);
  }

  private stopTimer(key: string): void {
    const e = this.entries.get(key);
    if (!e?.timer) return;
    clearInterval(e.timer);
    e.timer = null;
  }

  private onVisibility(): void {
    if (typeof document === 'undefined') return;
    if (document.hidden) {
      for (const key of this.entries.keys()) {
        this.stopTimer(key);
      }
      return;
    }
    for (const [key, e] of this.entries) {
      if (e.subs > 0) {
        void this.fetch(key);
        this.startTimer(key);
      }
    }
  }
}
