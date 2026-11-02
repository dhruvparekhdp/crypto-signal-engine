import { Injectable, signal } from '@angular/core';

export type LookId = 'a' | 'b' | 'c' | 'brutal';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  readonly look = signal<LookId>((localStorage.getItem('app_look') as LookId) || 'a');

  setLook(id: LookId): void {
    this.look.set(id);
    localStorage.setItem('app_look', id);
    document.documentElement.setAttribute('data-look', id === 'brutal' ? 'brutal' : id);
  }

  init(): void {
    this.setLook(this.look());
  }
}
