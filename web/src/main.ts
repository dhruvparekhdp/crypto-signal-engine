import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { AppComponent } from './app/app.component';
import { ThemeService } from './app/shared/ui/theme.service';

bootstrapApplication(AppComponent, appConfig)
  .then((ref) => ref.injector.get(ThemeService).init())
  .catch((err) => console.error(err));
