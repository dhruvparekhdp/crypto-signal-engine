import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom, timer } from 'rxjs';
import { plainApiError } from './api-errors';

const TIMEOUT_MS = 15_000;
const RETRIES = 2;

@Injectable({ providedIn: 'root' })
export class ApiClient {
  private readonly http = inject(HttpClient);

  async get<T>(path: string, init?: { etag?: string }): Promise<{ data: T; etag?: string; notModified?: boolean }> {
    return this.request<T>('GET', path, undefined, init?.etag);
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    const r = await this.request<T>('POST', path, body);
    return r.data;
  }

  private async request<T>(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
    etag?: string,
  ): Promise<{ data: T; etag?: string; notModified?: boolean }> {
    let lastErr: unknown;
    for (let attempt = 0; attempt <= RETRIES; attempt++) {
      try {
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (etag) {
          headers['If-None-Match'] = etag;
        }
        const obs =
          method === 'GET'
            ? this.http.get(path, { headers, observe: 'response', responseType: 'text' })
            : this.http.post(path, body ?? {}, { headers, observe: 'response', responseType: 'text' });
        const resp = await firstValueFrom(obs);
        if (resp.status === 304) {
          return { data: null as T, etag: resp.headers.get('ETag') ?? etag, notModified: true };
        }
        const text = resp.body ?? '';
        const data = text ? (JSON.parse(text) as T) : (null as T);
        return { data, etag: resp.headers.get('ETag') ?? undefined };
      } catch (e) {
        lastErr = e;
        if (e instanceof HttpErrorResponse && e.status >= 400 && e.status < 500 && e.status !== 408) {
          throw new Error(plainApiError(e.status, e.error ?? ''));
        }
        if (attempt < RETRIES) {
          await firstValueFrom(timer(1500));
        }
      }
    }
    if (lastErr instanceof HttpErrorResponse) {
      throw new Error(plainApiError(lastErr.status, String(lastErr.error ?? '')));
    }
    throw new Error(plainApiError(0, ''));
  }
}
