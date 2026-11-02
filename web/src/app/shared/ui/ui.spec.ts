import { MoneyComponent } from './money.component';
import { LevelChipComponent } from './level-chip.component';
import { RbarComponent } from './rbar.component';

describe('UI kit', () => {
  it('money formats INR', () => {
    const m = new MoneyComponent();
    expect(m.amount()).toBe(0);
  });

  it('rbar maps values to percent', () => {
    const r = new RbarComponent();
    expect(r.pct(50)).toBeGreaterThan(0);
  });

  it('level chip accepts severity', () => {
    const l = new LevelChipComponent();
    expect(l.level()).toBe(1);
  });
});
