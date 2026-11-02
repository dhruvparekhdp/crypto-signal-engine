import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

@Component({
  standalone: true,
  selector: 'app-sign-in',
  imports: [FormsModule, RouterLink],
  template: `
    <main class="sign-in">
      <h1>Sign in</h1>
      <p class="dim">Use the same login as Settings on the classic portal.</p>
      @if (message()) {
        <p class="err">{{ message() }}</p>
      }
      <form (submit)="submit($event)">
        <label>Password or session token</label>
        <input type="password" [(ngModel)]="token" name="token" autocomplete="current-password" />
        <button type="submit">Continue</button>
      </form>
      <a routerLink="/app/command/overview">Back to app</a>
    </main>
  `,
  styles: [
    `
      .sign-in {
        max-width: 22rem;
        margin: 4rem auto;
        padding: 1rem;
      }
      .dim {
        color: var(--dim);
      }
      .err {
        color: var(--warn);
      }
      input {
        width: 100%;
        margin: 0.5rem 0 1rem;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignInPage {
  token = '';
  readonly message = signal('');

  constructor(
    private readonly route: ActivatedRoute,
    private readonly router: Router,
  ) {}

  submit(ev: Event): void {
    ev.preventDefault();
    this.message.set('');
    if (!this.token.trim()) {
      this.message.set('Enter your password.');
      return;
    }
    sessionStorage.setItem('app_session_hint', this.token.trim());
    const next = this.route.snapshot.queryParamMap.get('next') || '/app/command/overview';
    void this.router.navigateByUrl(next);
  }
}
