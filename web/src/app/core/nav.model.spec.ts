import { NAV, appPath } from './nav.model';

describe('NAV', () => {
  it('mirrors portal hubs and tab counts', () => {
    expect(NAV.map((h) => h.id)).toEqual(['command', 'book', 'evidence', 'system']);
    expect(NAV[0].tabs.length).toBe(9);
  });

  it('builds app paths', () => {
    expect(appPath('command', 'overview')).toBe('/app/command/overview');
  });
});
