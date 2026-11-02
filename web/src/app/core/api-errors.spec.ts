import { plainApiError } from './api-errors';

describe('plainApiError', () => {
  it('maps 401 to sign-in message', () => {
    expect(plainApiError(401, '')).toContain('sign in');
  });

  it('maps network failure', () => {
    expect(plainApiError(0, '')).toContain('did not answer');
  });
});
